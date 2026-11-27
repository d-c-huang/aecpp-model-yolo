"""Auto-reference data, real media and persistent queue contract checks."""
import json
from contextlib import closing, nullcontext
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import auto_learning as learning
import dataset
import subtitles


def wait_for(predicate, timeout=8):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("auto learning worker did not reach expected state")


def example(text, seconds, start=0):
    return {"text": text, "duration": seconds, "start_ms": start,
            "end_ms": start+round(seconds*1000), "source_kind": "subtitle"}


def analysis(movie_id=1, mode="time", *, examples=None):
    return {"version": 1, "movie_id": movie_id, "title": f"影片{movie_id}",
            "film_hash": f"hash-{movie_id}", "mode": mode, "snapshot_hash": f"snapshot-{movie_id}",
            "duration_ms": 6000, "cut_candidates_ms": [0, 3000],
            "timing_examples": examples if examples is not None else [
                example("欢迎来到这里", 1.6), example("现在我们开始说话", 2., 1700), example("一起开始吧", 1.2, 3800)],
            "boards": [{"version": 1, "title": "参考", "characters": [], "shots": [
                {"id": "s1", "scene": "候选镜头 1", "speaker": "", "actor": "", "text": "欢迎来到这里",
                 "duration": 1.6, "action": "idle", "emotion": "neutral", "camera": "wide"}],
                "provenance": {"kind": "automatic-video-reference", "review_status": "unreviewed"}}]}


class ReferenceModelTests(unittest.TestCase):
    def test_only_timing_is_learned_and_explicit_validation_stays_out(self):
        first = analysis(1)
        validation = analysis(2, "val")
        model = learning.build_reference([first, validation])
        self.assertEqual(model["training_movie_ids"], [1])
        self.assertEqual(model["explicit_validation_movie_ids"], [2])
        self.assertEqual(model["example_count"], 3)
        self.assertEqual(model["validation"]["kind"], "independent-films")
        self.assertTrue(model["usable"])
        self.assertEqual(set(model["coefficients"]), {"seconds_per_character", "intercept"})
        self.assertNotIn("action", json.dumps(model))
        actual = sum(abs(learning.predict_reference_duration(model, row["text"])-row["duration"])
                     for row in validation["timing_examples"])/3
        self.assertAlmostEqual(actual, model["validation"]["mae_seconds"], places=5)

    def test_whole_film_holdout_and_single_film_limit_are_explicit(self):
        one = learning.build_reference([analysis(1)])
        self.assertEqual(one["validation"]["kind"], "none")
        two = learning.build_reference([analysis(1), analysis(2)])
        self.assertEqual(two["validation"]["kind"], "held-out-films")
        self.assertEqual(two["validation"]["movie_count"], 1)
        self.assertEqual(len(two["validation_movie_ids"]), 1)
        self.assertEqual(two["movie_count"], 2)
        self.assertIsNone(learning.predict_reference_duration(learning.build_reference([analysis(1, "val")]), "测试文字"))

    def test_weak_holdout_keeps_baseline_and_all_duplicate_validation_wins(self):
        train = analysis(1, examples=[example("同一条较长测试字幕", .7)]*3)
        val = analysis(2, "val", examples=[example("同一条较长测试字幕", learning._baseline("同一条较长测试字幕"))])
        model = learning.build_reference([train, val])
        self.assertFalse(model["usable"])
        self.assertFalse(model["validation"]["improved"])
        val["film_hash"] = train["film_hash"]
        model = learning.build_reference([train, val])
        self.assertEqual(model["example_count"], 0)

    def test_model_loading_rejects_corruption_and_predictions_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(learning.load_reference(root))
            model = learning.build_reference([analysis()])
            learning._write_json(root/"auto_learning/reference.json", model)
            loaded = learning.load_reference(root)
            text = "长句测试"*100
            predicted = learning.predict_reference_duration(loaded, text)
            self.assertGreaterEqual(predicted, learning._baseline(text)*.6)
            self.assertLessEqual(predicted, learning._baseline(text)*1.4)
            model["coefficients"]["intercept"] = "bad"
            learning._write_json(root/"auto_learning/reference.json", model)
            with self.assertRaises(ValueError):
                learning.load_reference(root)


class AutoQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/"项目"
        dataset.initialize(self.root)
        self.managers = []
        for number in (1, 2):
            path = Path(self.temp.name)/f"{number}.dat"
            path.write_bytes(f"source-{number}".encode())
            dataset.register_movie(self.root, path, "time", lambda _: None)

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        for manager in self.managers:
            wait_for(lambda: not manager.busy)
        self.temp.cleanup()

    def manager(self, analyser=None):
        def matched(root, movie, report, check):
            return self.match_analysis(analysis(movie))
        manager = learning.AutoLearningManager(self.root, analyser=analyser or matched)
        self.managers.append(manager)
        return manager

    def match_analysis(self, value):
        with dataset.connect(self.root) as db:
            value["film_hash"] = db.execute("SELECT digest FROM movies WHERE id=?", (value["movie_id"],)).fetchone()[0]
        return value

    def test_idle_wake_does_not_spawn_workers_or_take_storage_locks(self):
        manager = self.manager()
        with patch.object(manager, "_project_lock", side_effect=AssertionError("no queued work")):
            manager.wake()
            manager.wake()
        self.assertFalse(manager.busy)

    def test_autostart_dedup_real_model_publication_and_portable_paths(self):
        manager = self.manager()
        ids = manager.enqueue([1])
        wait_for(lambda: not manager.busy)
        self.assertEqual(len(ids), 1)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"], "completed")
        self.assertTrue(Path(row["board_path"]).is_file())
        self.assertEqual(row["example_count"], 3)
        self.assertTrue(learning.load_reference(self.root)["usable"])
        self.assertFalse((self.root/"story_learning/director.json").exists())
        with closing(sqlite3.connect(manager.db_path)) as db:
            stored = json.loads(db.execute("SELECT data FROM jobs").fetchone()[0])
        self.assertFalse(Path(stored["board_paths"][0]).is_absolute())
        self.assertEqual(manager.enqueue([1]), [])
        manager.close()
        reopened = self.manager()
        self.assertEqual(reopened.enqueue([1]), [])
        self.assertEqual(reopened.list_jobs()[0]["attempts"], 1)

    def test_pause_resume_and_close_never_publish_interrupted_model(self):
        entered = threading.Event()
        def slow(root, movie, report, check):
            entered.set()
            while True:
                check()
                time.sleep(.005)
        manager = self.manager(slow)
        manager.enqueue([1, 2])
        self.assertTrue(entered.wait(3))
        manager.pause()
        wait_for(lambda: not manager.busy)
        self.assertTrue(manager.paused)
        self.assertEqual({job["status"] for job in manager.list_jobs()}, {"paused"})
        self.assertIsNone(learning.load_reference(self.root))
        manager._analyser = lambda root, movie, report, check: self.match_analysis(analysis(movie))
        manager.resume()
        wait_for(lambda: not manager.busy)
        self.assertEqual({job["status"] for job in manager.list_jobs()}, {"completed"})
        self.assertEqual(learning.load_reference(self.root)["movie_count"], 2)

    def test_cancel_failure_retry_and_no_subtitle_status(self):
        manager = self.manager(lambda *args: (_ for _ in ()).throw(ValueError("原片不可读")))
        ids = manager.enqueue([1])
        wait_for(lambda: not manager.busy)
        self.assertEqual(manager.list_jobs()[0]["status"], "failed")
        manager._analyser = lambda root, movie, report, check: self.match_analysis(analysis(movie, examples=[]))
        manager.retry(ids)
        wait_for(lambda: not manager.busy)
        self.assertEqual(manager.list_jobs()[0]["status"], "needs_input")
        self.assertFalse(learning.load_reference(self.root)["usable"])
        manager.pause()
        second = manager.enqueue([2])
        manager.cancel(second)
        self.assertEqual(manager.list_jobs()[0]["status"], "cancelled")
        manager._analyser = lambda root, movie, report, check: self.match_analysis(analysis(movie))
        manager.retry(second)
        manager.resume()
        wait_for(lambda: not manager.busy)
        self.assertEqual(manager.list_jobs()[0]["status"], "completed")

    def test_force_reanalysis_replaces_prior_movie_examples(self):
        manager = self.manager()
        manager.enqueue([1])
        wait_for(lambda: not manager.busy)
        manager._analyser = lambda root, movie, report, check: self.match_analysis(analysis(movie, examples=[example("修正后的字幕", 3.)]))
        manager.enqueue([1], force=True)
        wait_for(lambda: not manager.busy)
        self.assertEqual(learning.load_reference(self.root)["example_count"], 1)
        self.assertEqual(manager.list_jobs()[0]["attempts"], 2)

    def test_close_active_worker_and_crash_recovery_need_explicit_resume(self):
        started = threading.Event()
        def slow(root, movie, report, check):
            started.set()
            while True:
                check()
                time.sleep(.005)
        manager = self.manager(slow)
        manager.enqueue([1])
        self.assertTrue(started.wait(3))
        manager.close()
        wait_for(lambda: not manager.busy)
        self.assertIsNone(learning.load_reference(self.root))
        # Simulate a process killed before it could persist its paused status.
        with closing(sqlite3.connect(manager.db_path)) as db, db:
            row = json.loads(db.execute("SELECT data FROM jobs").fetchone()[0])
            row["status"] = "running"
            db.execute("UPDATE jobs SET status='running',data=?", (json.dumps(row),))
            db.execute("UPDATE meta SET value='0'")
        reopened = self.manager()
        self.assertFalse(reopened.busy)
        self.assertTrue(reopened.paused)
        self.assertEqual(reopened.list_jobs()[0]["status"], "paused")
        reopened.resume()
        wait_for(lambda: not reopened.busy)
        self.assertEqual(reopened.list_jobs()[0]["status"], "completed")

    def test_changed_validation_group_excludes_previously_analysed_movie(self):
        manager = self.manager()
        manager.enqueue([1])
        wait_for(lambda: not manager.busy)
        self.assertEqual(learning.load_reference(self.root)["training_movie_ids"], [1])
        with dataset.connect(self.root) as db:
            db.execute("UPDATE movies SET mode='val' WHERE id=1")
        manager.enqueue([2])
        wait_for(lambda: not manager.busy)
        reference = learning.load_reference(self.root)
        self.assertEqual(reference["training_movie_ids"], [2])
        self.assertEqual(reference["explicit_validation_movie_ids"], [1])

    def test_project_lock_blocks_second_worker_and_wake_resumes_authorized_queue(self):
        folder = self.root/"auto_learning"
        folder.mkdir()
        lock = learning._ProjectLock(folder/".worker.lock")
        self.assertIsNotNone(lock.fd)
        try:
            manager = self.manager()
            manager.enqueue([1])
            self.assertFalse(manager.busy)
            self.assertEqual(manager.list_jobs()[0]["status"], "queued")
            manager.wake()
            self.assertFalse(manager.busy)
        finally:
            lock.close()
        manager.wake()
        wait_for(lambda: not manager.busy)
        self.assertEqual(manager.list_jobs()[0]["status"], "completed")

    def test_second_manager_does_not_recover_or_interrupt_live_worker(self):
        started, release = threading.Event(), threading.Event()
        def slow(root, movie, report, check):
            started.set()
            while not release.wait(.01):
                check()
            return self.match_analysis(analysis(movie))
        first = self.manager(slow)
        first.enqueue([1])
        self.assertTrue(started.wait(3))
        second = self.manager()
        self.assertEqual(second.list_jobs()[0]["status"], "running")
        second.wake()
        self.assertFalse(second.busy)
        second.close()
        self.assertFalse(first.paused)
        release.set()
        wait_for(lambda: not first.busy)
        self.assertEqual(first.list_jobs()[0]["status"], "completed")


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg generates real video fixtures")
class AutoMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder/"工程"
        dataset.initialize(self.root)
        self.source = self.folder/"短片.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=5:duration=6",
                        "-c:v", "ffv1", str(self.source)], check=True, capture_output=True)
        self.movie, _ = dataset.import_movie(self.root, self.source, "time", lambda _: None)

    def subtitles(self):
        subtitles.store_track(self.root, self.movie, "manual", "中文", "v1", [
            (0, 1500, "欢迎来到这里"), (1700, 3700, "现在我们开始说话"), (4000, 5500, "一起开始吧")])

    def test_real_media_analysis_preserves_timing_and_role_evidence(self):
        self.subtitles()
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO scripts VALUES(1,?,'测试','test')", (self.movie,))
            db.execute("INSERT INTO script_lines VALUES(1,1,'小明','欢迎来到这里',0,1500)")
        result = learning.analyse_movie(self.root, self.movie)
        self.assertEqual(len(result["timing_examples"]), 3)
        self.assertEqual(result["duration_ms"], 6000)
        board = result["boards"][0]
        self.assertEqual(board["characters"][0]["name"], "小明")
        self.assertEqual(board["shots"][0]["duration"], 1.5)
        self.assertEqual(board["shots"][0]["role_evidence"], "script-match")
        self.assertFalse(board["shots"][1]["speaker"])
        self.assertEqual(board["provenance"]["review_status"], "unreviewed")
        from story_pipeline import validate_storyboard
        validate_storyboard(board)

    def test_no_subtitles_and_changed_source_are_not_training_success(self):
        result = learning.analyse_movie(self.root, self.movie)
        self.assertEqual(result["timing_examples"], [])
        self.assertEqual(result["boards"], [])
        self.assertTrue(result["cut_candidates_ms"])
        with self.source.open("ab") as file:
            file.write(b"changed")
        with self.assertRaisesRegex(ValueError, "原片已变化"):
            learning.analyse_movie(self.root, self.movie)

    def test_duplicate_tracks_and_overlapping_subtitles_do_not_double_train(self):
        self.subtitles()
        subtitles.store_track(self.root, self.movie, "platform", "English", "v2", [
            (0, 1500, "Welcome here"), (1700, 3700, "Now we speak"), (4000, 5500, "Let us begin")])
        result = learning.analyse_movie(self.root, self.movie)
        self.assertEqual(len(result["timing_examples"]), 3)
        self.assertEqual(result["timing_examples"][0]["text"], "欢迎来到这里")
        subtitles.store_track(self.root, self.movie, "manual", "中文", "v3", [
            (0, 2500, "重叠的第一句"), (1000, 3000, "重叠的第二句"), (4000, 5500, "一起开始吧")])
        result = learning.analyse_movie(self.root, self.movie)
        self.assertEqual(len(result["timing_examples"]), 1)
        self.assertEqual(len(result["boards"][0]["shots"]), 3)

    def test_long_video_is_partitioned_without_losing_timed_examples(self):
        from PIL import Image
        cues = [(index*2000, index*2000+1500, f"这是第{index}句需要保留的对白") for index in range(1201)]
        subtitles.store_track(self.root, self.movie, "manual", "中文", "long", cues)
        fake = SimpleNamespace(duration=2500., samples=lambda times: iter([(0., Image.new("RGB", (16, 16)))]))
        with patch.object(learning, "open_movie", return_value=nullcontext(fake)):
            result = learning.analyse_movie(self.root, self.movie)
        self.assertEqual(len(result["timing_examples"]), 1201)
        self.assertEqual(len(result["boards"]), 3)
        self.assertEqual(sum(len(board["shots"]) for board in result["boards"]), 1201)
        self.assertEqual(len({board["provenance"]["source_hash"] for board in result["boards"]}), 1)
        from story_pipeline import validate_storyboard
        for board in result["boards"]:
            validate_storyboard(board)
            self.assertLessEqual(len(board["shots"]), 600)


if __name__ == "__main__":
    unittest.main()
