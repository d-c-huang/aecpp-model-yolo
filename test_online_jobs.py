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


def iqiyi_metadata(url="https://www.iqiyi.com/v_fixture0.html"):
    return {"site": "iqiyi", "url": url, "tvid": "123456789", "vid": "a" * 32,
            "duration": 12.5, "title": "公开影片"}


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

    def test_learning_kind_is_frozen_and_legacy_identity_stays_compatible(self):
        received = []
        def importer(root, url, mode, report, *, height, subtitles, stop):
            received.append((height, subtitles))
            return self.import_ok(root, url, mode, report)
        manager = self.manager(importer)
        legacy_id = manager.add("https://example.com/film")["added"][0]
        self.assertFalse(manager.add("https://example.com/film", learning_kind="movie-temporal")["added"])
        automatic_id = manager.add("https://example.com/film", learning_kind="story-reference")["added"][0]
        with self.assertRaises(ValueError):
            manager.add("https://example.com/bad", learning_kind="unknown")
        manager.start()
        wait_for(lambda: not manager.busy)
        rows = {row["id"]: row for row in manager.list_jobs()}
        self.assertNotIn("learning_kind", rows[legacy_id]["config"])
        self.assertEqual(rows[automatic_id]["config"]["learning_kind"], "story-reference")
        self.assertEqual(len(received), 2)
        previous = rows[automatic_id]
        self.assertEqual(manager.acknowledge_learning([legacy_id, automatic_id]), [automatic_id])
        self.assertEqual(manager.acknowledge_learning([automatic_id]), [])
        current = next(row for row in manager.list_jobs() if row["id"] == automatic_id)
        self.assertTrue(current["learning_dispatched"])
        for key in ("status", "config", "fingerprint"):
            self.assertEqual(current[key], previous[key])
        manager.close()
        reopened = self.manager()
        self.assertTrue(next(row for row in reopened.list_jobs() if row["id"] == automatic_id)["learning_dispatched"])

    def test_discovery_metadata_survives_reopen_and_reaches_explicit_importer(self):
        received = []
        def importer(root, url, mode, report, *, height, subtitles, stop, source_metadata):
            received.append(source_metadata)
            return self.import_ok(root, url, mode, report)
        metadata = iqiyi_metadata()
        manager = self.manager(importer)
        job_id = manager.add(metadata["url"])["added"][0]
        original = manager.list_jobs()[0]
        manager.attach_discovery(job_id, {"id": "candidate-1", "round_id": "round-1", "continuous": True,
                                          "video_metadata": metadata})
        attached = manager.list_jobs()[0]
        self.assertEqual(attached["discovery"]["video_metadata"], metadata)
        self.assertTrue(attached["discovery"]["continuous"])
        self.assertEqual(attached["fingerprint"], original["fingerprint"])
        self.assertEqual(manager.add(metadata["url"])["duplicates"], [metadata["url"]])
        manager.close()
        reopened = self.manager(importer)
        self.assertTrue(reopened.paused)
        self.assertEqual(reopened.list_jobs()[0]["discovery"]["video_metadata"], metadata)
        reopened.resume()
        wait_for(lambda: not reopened.busy)
        self.assertEqual(received, [metadata])
        self.assertEqual(reopened.list_jobs()[0]["status"], "completed")

    def test_discovery_metadata_whitelists_values_before_kwargs_importer(self):
        received = []
        def importer(root, url, mode, report, **kwargs):
            received.append(kwargs.get("source_metadata"))
            return self.import_ok(root, url, mode, report)
        metadata = iqiyi_metadata()
        manager = self.manager(importer)
        job_id = manager.add(metadata["url"])["added"][0]
        manager.attach_discovery(job_id, {"video_metadata": {**metadata, "exec": "untrusted command",
                                                            "ydl_opts": {"exec_cmd": "untrusted command"}}})
        self.assertEqual(manager.list_jobs()[0]["discovery"]["video_metadata"], metadata)
        manager.start()
        wait_for(lambda: not manager.busy)
        self.assertEqual(received, [metadata])
        self.assertEqual(manager.list_jobs()[0]["status"], "completed")

    def test_discovery_metadata_preserves_legacy_importer_compatibility(self):
        received = []
        def importer(root, url, mode, report, *, height, subtitles, stop):
            received.append(url)
            return self.import_ok(root, url, mode, report)
        metadata = iqiyi_metadata()
        manager = self.manager(importer)
        job_id = manager.add(metadata["url"])["added"][0]
        manager.attach_discovery(job_id, {"video_metadata": metadata})
        manager.start()
        wait_for(lambda: not manager.busy)
        self.assertEqual(received, [metadata["url"]])
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"], "completed")
        self.assertIn("使用原网页解析", row["log_tail"])

    def test_absent_or_invalid_discovery_metadata_omits_importer_keyword(self):
        received = []
        def importer(root, url, mode, report, **kwargs):
            received.append((url, "source_metadata" in kwargs))
            return self.import_ok(root, url, mode, report)
        metadata = iqiyi_metadata()
        manager = self.manager(importer)
        ids = manager.add(metadata["url"] + "\nhttps://www.iqiyi.com/v_other.html")["added"]
        manager.attach_discovery(ids[0], {"id": "valid-without-metadata"})
        manager.attach_discovery(ids[1], {"video_metadata": metadata})
        rows = {row["id"]: row for row in manager.list_jobs()}
        self.assertNotIn("video_metadata", rows[ids[1]]["discovery"])
        self.assertIn("不匹配", rows[ids[1]]["discovery"]["metadata_warning"])
        manager.start()
        wait_for(lambda: not manager.busy)
        self.assertEqual(len(received), 2)
        self.assertTrue(all(not has_metadata for url, has_metadata in received))
        self.assertTrue(all(row["status"] == "completed" for row in manager.list_jobs()))
        invalid = next(row for row in manager.list_jobs() if row["id"] == ids[1])
        self.assertIn("使用原网页解析", invalid["log_tail"])

    def test_stored_discovery_metadata_is_revalidated_before_import(self):
        received = []
        def importer(root, url, mode, report, **kwargs):
            received.append("source_metadata" in kwargs)
            return self.import_ok(root, url, mode, report)
        metadata = iqiyi_metadata()
        manager = self.manager(importer)
        job_id = manager.add(metadata["url"])["added"][0]
        manager.attach_discovery(job_id, {"video_metadata": metadata})
        manager.close()
        with closing(sqlite3.connect(manager.db_path)) as db:
            record = json.loads(db.execute("SELECT data FROM online_jobs WHERE id=?", (job_id,)).fetchone()[0])
            record["discovery"]["video_metadata"]["vid"] = "invalid; arbitrary command"
            db.execute("UPDATE online_jobs SET data=? WHERE id=?", (json.dumps(record), job_id))
            db.commit()
        reopened = self.manager(importer)
        reopened.resume()
        wait_for(lambda: not reopened.busy)
        self.assertEqual(received, [False])
        row = reopened.list_jobs()[0]
        self.assertEqual(row["status"], "completed")
        self.assertIn("vid 无效", row["log_tail"])

    def test_cancel_learning_running_survives_completion_and_reopen(self):
        entered, release = threading.Event(), threading.Event()
        def importer(root, url, mode, report, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Test release did not arrive")
            return self.import_ok(root, url, mode, report)
        manager = self.manager(importer)
        job_id = manager.add("https://example.com/film", learning_kind="story-reference")["added"][0]
        manager.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(manager.cancel_learning([job_id]), [job_id])
        self.assertEqual(manager.cancel_learning([job_id]), [])
        release.set()
        wait_for(lambda: not manager.busy)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"], "completed")
        self.assertTrue(row["learning_cancelled"])
        completion = next(event for event in manager.drain_events() if event["type"] == "completed")
        self.assertFalse(completion["learn"])
        manager.close()
        reopened = self.manager()
        self.assertTrue(reopened.list_jobs()[0]["learning_cancelled"])

    def test_retry_reauthorizes_cancelled_learning_but_preserves_acknowledgement(self):
        def importer(root, url, mode, report, **kwargs):
            if url.endswith("bad"):
                raise ValueError("temporary failure")
            return self.import_ok(root, url, mode, report)
        manager = self.manager(importer)
        ids = manager.add("https://example.com/ok\nhttps://example.com/bad", learning_kind="story-reference")["added"]
        manager.start()
        wait_for(lambda: not manager.busy)
        manager.acknowledge_learning([ids[0]])
        manager.cancel_learning(ids)
        manager.retry([ids[1]], request_version=9)
        rows = {row["id"]: row for row in manager.list_jobs()}
        self.assertTrue(rows[ids[0]]["learning_dispatched"])
        self.assertTrue(all(not row.get("learning_cancelled") and row["request_version"] == 9 for row in rows.values()))

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
