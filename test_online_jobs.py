"""Queue lifecycle tests use fake importers and disposable projects only."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import online_jobs as jobs


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Timed out waiting for queue state")


class OnlineJobsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yingge-online-queue-")
        self.root = Path(self.temporary.name)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        for manager in self.managers:
            wait_for(lambda: not manager.busy)
        self.temporary.cleanup()

    def manager(self, importer=None):
        manager = jobs.OnlineJobManager(self.root, importer=importer or self.import_ok)
        self.managers.append(manager)
        return manager

    @staticmethod
    def import_ok(root, url, mode, report, **kwargs):
        report("视频已读入")
        return dict(movie_id=1, title="测试影片", path=str(root / "movie.mp4"), source_url=url)

    @staticmethod
    def stopped_import(root, url, mode, report, *, stop, **kwargs):
        report("下载中")
        if not stop.wait(3):
            raise AssertionError("Importer was not stopped")
        raise RuntimeError("import stopped")

    def test_batch_validation_dedup_and_configuration(self):
        manager = self.manager()
        self.assertFalse(manager.paused)
        result = manager.add("HTTPS://EXAMPLE.com:443/film#one\n\nhttps://example.com/film#two\nfile:///film.mp4\nhttps://example.com/other?q=1", request_version=7)
        self.assertEqual(2, len(result["added"]))
        self.assertEqual(1, len(result["duplicates"]))
        self.assertEqual(4, result["errors"][0]["line"])
        records = manager.list_jobs()
        self.assertEqual(1, len({row["batch_id"] for row in records}))
        self.assertTrue(all(row["request_version"] == 7 for row in records))
        self.assertEqual([], manager.add("https://example.com/film")["added"])
        self.assertEqual(1, len(manager.add("https://example.com/film", height=1080)["added"]))
        self.assertEqual(1, len(manager.add("https://example.com/film", learn=False)["added"]))
        with self.assertRaises(ValueError):
            manager.add("https://example.com/new", height=123)
        self.assertTrue(manager.has_pending)
        self.assertFalse(manager.busy)

    def test_serial_import_events_progress_and_failure_retry(self):
        calls, active, peak = [], 0, 0
        failed_once = set()
        def importer(root, url, mode, report, *, on_progress, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append(url)
            try:
                on_progress(dict(phase="downloading", percent=42, total_bytes=100, downloaded_bytes=42,
                                 speed=8, eta=7, title="读取中的影片"))
                report("测试后台进度")
                if url.endswith("bad") and url not in failed_once:
                    failed_once.add(url)
                    raise ValueError("来源暂时不可用")
                on_progress(dict(phase="importing", percent=None, speed=None, eta=None))
                current = next(row for row in manager.list_jobs() if row["url"] == url)
                self.assertIsNone(current["progress"])
                self.assertIsNone(current["speed"])
                return self.import_ok(root, url, mode, report)
            finally:
                active -= 1
        manager = self.manager(importer)
        ids = manager.add("https://example.com/one\nhttps://example.com/bad\nhttps://example.com/two", request_version=3)["added"]
        manager.start()
        wait_for(lambda: not manager.busy)
        self.assertEqual(1, peak)
        self.assertEqual(3, len(calls))
        self.assertEqual(["completed", "failed", "completed"], [next(row for row in manager.list_jobs() if row["id"] == value)["status"] for value in ids])
        events = [event for event in manager.drain_events() if event["type"] == "completed"]
        self.assertEqual(2, len(events))
        self.assertTrue(all(event["learn"] and event["request_version"] == 3 and event["batch_id"] for event in events))
        self.assertEqual([], manager.drain_events())
        manager.retry([ids[1]], request_version=8)
        self.assertTrue(all(row["request_version"] == 8 for row in manager.list_jobs()))
        manager.start()
        wait_for(lambda: not manager.busy)
        self.assertTrue(all(row["status"] == "completed" for row in manager.list_jobs()))
        self.assertEqual(2, next(row for row in manager.list_jobs() if row["id"] == ids[1])["attempts"])
        self.assertEqual(8, next(event for event in manager.drain_events() if event["type"] == "completed")["request_version"])

    def test_pause_resume_stops_current_and_retains_configuration(self):
        started = threading.Event()
        calls = []
        def importer(root, url, mode, report, **kwargs):
            calls.append((url, mode, kwargs["height"], kwargs["subtitles"]))
            if len(calls) == 1:
                started.set()
                return self.stopped_import(root, url, mode, report, **kwargs)
            return self.import_ok(root, url, mode, report, **kwargs)
        manager = self.manager(importer)
        ids = manager.add("https://example.com/one\nhttps://example.com/two", mode="train", height=1080, subtitles=False)["added"]
        manager.start()
        self.assertTrue(started.wait(2))
        manager.pause()
        wait_for(lambda: not manager.busy)
        self.assertTrue(manager.paused)
        self.assertTrue(all(row["status"] == "paused" for row in manager.list_jobs()))
        extra = manager.add("https://example.com/three")["added"][0]
        self.assertEqual("paused", next(row for row in manager.list_jobs() if row["id"] == extra)["status"])
        manager.resume(request_version=9)
        wait_for(lambda: not manager.busy)
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(all(row["status"] == "completed" for row in manager.list_jobs()))
        self.assertTrue(all(row["request_version"] == 9 for row in manager.list_jobs()))
        self.assertFalse(manager.has_pending)

    def test_resume_before_previous_importer_stops_requeues_once(self):
        started, stopped, release = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def importer(root, url, mode, report, *, stop, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                started.set()
                self.assertTrue(stop.wait(2))
                stopped.set()
                self.assertTrue(release.wait(2))
                raise RuntimeError("stopped")
            return self.import_ok(root, url, mode, report)
        manager = self.manager(importer)
        manager.add("https://example.com/one")
        manager.start()
        self.assertTrue(started.wait(2))
        manager.pause()
        self.assertTrue(stopped.wait(2))
        manager.resume(request_version=5)
        release.set()
        wait_for(lambda: not manager.busy)
        self.assertEqual(2, len(calls))
        self.assertEqual("completed", manager.list_jobs()[0]["status"])
        self.assertEqual(5, manager.list_jobs()[0]["request_version"])

    def test_reopen_resume_reauthorizes_already_completed_batch_members(self):
        second_started = threading.Event()
        def importer(root, url, mode, report, **kwargs):
            if url.endswith("two"):
                second_started.set()
                return self.stopped_import(root, url, mode, report, **kwargs)
            return self.import_ok(root, url, mode, report, **kwargs)
        manager = self.manager(importer)
        manager.add("https://example.com/one\nhttps://example.com/two", request_version=7)
        manager.start()
        self.assertTrue(second_started.wait(2))
        manager.close()
        wait_for(lambda: not manager.busy)
        reopened = self.manager()
        reopened.resume(request_version=0)
        wait_for(lambda: not reopened.busy)
        self.assertTrue(all(row["request_version"] == 0 for row in reopened.list_jobs()))
        self.assertTrue(all(row["status"] == "completed" for row in reopened.list_jobs()))
        self.assertEqual(1, len([event for event in reopened.drain_events() if event["type"] == "completed"]))

    def test_close_is_prompt_busy_lasts_until_importer_exit_and_reopen_pauses(self):
        started, stopped, release = threading.Event(), threading.Event(), threading.Event()
        def importer(root, url, mode, report, *, stop, **kwargs):
            started.set()
            stop.wait(3)
            stopped.set()
            release.wait(3)
            raise RuntimeError("stopped")
        manager = self.manager(importer)
        manager.add("https://example.com/one\nhttps://example.com/two")
        manager.start()
        self.assertTrue(started.wait(2))
        before = time.monotonic()
        manager.close()
        self.assertLess(time.monotonic() - before, .75)
        self.assertTrue(stopped.wait(1))
        self.assertTrue(manager.busy)
        release.set()
        wait_for(lambda: not manager.busy)
        reopened = self.manager()
        self.assertTrue(reopened.paused)
        self.assertFalse(reopened.busy)
        self.assertTrue(all(row["status"] == "paused" for row in reopened.list_jobs()))
        reopened.resume()
        wait_for(lambda: not reopened.busy)
        self.assertTrue(all(row["status"] == "completed" for row in reopened.list_jobs()))

    def test_existing_live_owner_is_not_recovered_or_stolen(self):
        started = threading.Event()
        def importer(*args, **kwargs):
            started.set()
            return self.stopped_import(*args, **kwargs)
        owner = self.manager(importer)
        job_id = owner.add("https://example.com/one")["added"][0]
        owner.start()
        self.assertTrue(started.wait(2))
        observer = self.manager()
        self.assertEqual("running", observer.list_jobs()[0]["status"])
        self.assertFalse(observer.paused)
        with self.assertRaisesRegex(ValueError, "另一个窗口"):
            observer.start()
        observer.cancel([job_id])
        wait_for(lambda: not owner.busy)
        self.assertEqual("cancelled", owner.list_jobs()[0]["status"])
        observer.retry([job_id])
        observer.start()
        wait_for(lambda: not observer.busy)
        self.assertEqual("completed", observer.list_jobs()[0]["status"])

    def test_other_process_cannot_recover_running_owner(self):
        started, release = threading.Event(), threading.Event()
        def importer(root, url, mode, report, *, stop, **kwargs):
            started.set()
            while not release.wait(.05):
                if stop.is_set():
                    raise RuntimeError("stopped")
            return self.import_ok(root, url, mode, report)
        manager = self.manager(importer)
        manager.add("https://example.com/one")
        manager.start()
        self.assertTrue(started.wait(2))
        try:
            script = "import sys,json; from online_jobs import OnlineJobManager; m=OnlineJobManager(sys.argv[1]); print(json.dumps([m.paused,m.list_jobs()[0]['status']])); m.close()"
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(self.root)], capture_output=True, text=True, timeout=12)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([False, "running"], json.loads(result.stdout.strip()))
            self.assertEqual("running", manager.list_jobs()[0]["status"])
        finally:
            release.set()
        wait_for(lambda: not manager.busy)

    def test_cancel_at_commit_preserves_result_but_suppresses_learning(self):
        started = threading.Event()
        def importer(root, url, mode, report, *, stop, **kwargs):
            started.set()
            self.assertTrue(stop.wait(2))
            return self.import_ok(root, url, mode, report)
        manager = self.manager(importer)
        job_id = manager.add("https://example.com/one")["added"][0]
        manager.start()
        self.assertTrue(started.wait(2))
        manager.cancel([job_id])
        wait_for(lambda: not manager.busy)
        row = manager.list_jobs()[0]
        self.assertEqual("completed", row["status"])
        self.assertFalse(row["learn"])
        self.assertTrue(row["config"]["learn"])
        event = next(event for event in manager.drain_events() if event["type"] == "completed")
        self.assertFalse(event["learn"])
        self.assertEqual(1, event["result"]["movie_id"])

    def test_remove_only_records_and_keeps_media_and_logs(self):
        movie = self.root / "movie.mp4"
        movie.write_bytes(b"existing movie")
        manager = self.manager()
        ids = manager.add("https://example.com/one\nhttps://example.com/two")["added"]
        manager.start()
        wait_for(lambda: not manager.busy)
        log = Path(manager.list_jobs()[0]["log_path"])
        self.assertTrue(log.is_file())
        self.assertEqual(ids, manager.remove(ids))
        self.assertEqual([], manager.list_jobs())
        self.assertEqual(b"existing movie", movie.read_bytes())
        self.assertTrue(log.is_file())

    def test_running_removal_is_atomic_and_requires_cancel(self):
        started = threading.Event()
        def importer(*args, **kwargs):
            started.set()
            return self.stopped_import(*args, **kwargs)
        manager = self.manager(importer)
        ids = manager.add("https://example.com/one\nhttps://example.com/two")["added"]
        manager.start()
        self.assertTrue(started.wait(2))
        with self.assertRaises(ValueError):
            manager.remove(ids)
        self.assertEqual(2, len(manager.list_jobs()))
        manager.cancel(ids)
        wait_for(lambda: not manager.busy)
        self.assertTrue(all(row["status"] == "cancelled" for row in manager.list_jobs()))
        self.assertEqual(ids, manager.remove(ids))

    def test_logs_and_progress_events_are_bounded(self):
        def importer(root, url, mode, report, *, on_progress, **kwargs):
            for index in range(100):
                report("下载中 " + "画" * 3500)
                on_progress(dict(phase="downloading", percent=float("nan"), speed=float("inf"), eta=-1))
            row = manager.list_jobs()[0]
            self.assertIsNone(row["progress"])
            self.assertIsNone(row["speed"])
            self.assertIsNone(row["eta"])
            return self.import_ok(root, url, mode, report)
        manager = self.manager(importer)
        manager.add("https://example.com/one")
        manager.start()
        wait_for(lambda: not manager.busy)
        row = manager.list_jobs()[0]
        self.assertEqual("completed", row["status"])
        self.assertLessEqual(Path(row["log_path"]).stat().st_size, jobs.LOG_LIMIT)
        self.assertLessEqual(len(row["log_tail"]), jobs.TAIL_LIMIT)
        self.assertLessEqual(len(manager.drain_events()), 2)

    def test_legacy_history_requires_registered_digest_and_never_auto_learns(self):
        with closing(sqlite3.connect(self.root / "annotations.sqlite")) as db:
            db.execute("CREATE TABLE movies(id INTEGER PRIMARY KEY,digest TEXT,name TEXT,mode TEXT,source_path TEXT)")
            db.execute("INSERT INTO movies VALUES(1,'registered','既有影片','train',?)", (str(self.root / "existing.mp4"),))
            db.commit()
        for name, digest in (("valid", "registered"), ("orphan", "unregistered")):
            folder = self.root / "online_media" / name
            folder.mkdir(parents=True)
            (folder / "completed.json").write_text(json.dumps(dict(source_url="https://example.com/" + name,
                                                                  sha256=digest, height=720, subtitles=True,
                                                                  metadata={"duration": 12, "width": 720})), encoding="utf-8")
            (folder / "preview.jpg").write_bytes(b"local preview")
        manager = self.manager()
        records = manager.list_jobs()
        self.assertEqual(1, len(records))
        self.assertEqual("completed", records[0]["status"])
        self.assertEqual("既有影片", records[0]["title"])
        self.assertFalse(records[0]["learn"])
        self.assertEqual(12, records[0]["result"]["metadata"]["duration"])
        self.assertEqual(b"local preview", Path(records[0]["result"]["thumbnail_path"]).read_bytes())
        self.assertEqual([], manager.drain_events())
        self.assertFalse(manager.has_pending)
        manager.remove([records[0]["id"]])
        self.assertEqual([], self.manager().list_jobs())

    def test_queued_jobs_reopen_paused_and_completed_history_is_not_reemitted(self):
        manager = self.manager()
        manager.add("https://example.com/completed")
        manager.start()
        wait_for(lambda: not manager.busy)
        manager.add("https://example.com/queued", request_version=7)
        manager.close()
        reopened = self.manager()
        self.assertTrue(reopened.paused)
        self.assertCountEqual(["completed", "paused"], [row["status"] for row in reopened.list_jobs()])
        self.assertEqual([], reopened.drain_events())


if __name__ == "__main__":
    unittest.main()
