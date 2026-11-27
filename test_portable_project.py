"""Migration tests use only temporary projects, caches and tiny model fixtures."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import cache_storage
import dataset
from online_jobs import OnlineJobManager, _ProjectLock
import portable_project as portable


class PortableProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aecpp-portable-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "原工程"
        dataset.initialize(self.root)
        self.archive = self.base / "project.zip"
        self.target = self.base / "新电脑工程"
        self.source = self.base / "外部原片.mp4"
        self.source.write_bytes(b"local source fixture")
        value = self.source.stat()
        self.frame = self.root / "frames" / "one.jpg"
        self.frame.write_bytes(b"frame fixture")
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path,source_size,source_mtime) VALUES(1,'film','影片','train',?,?,?)",
                       (str(self.source), value.st_size, value.st_mtime_ns))
            db.execute("INSERT INTO frames(movie,slot,ms,path,split,state,boxes,voices) VALUES(1,0,0,'frames/one.jpg','train','reviewed','[[0,0,0,1,1]]','[]')")
            db.execute("INSERT INTO movie_cast VALUES(1,'角色甲',0)")
            db.execute("INSERT INTO subtitle_tracks VALUES(1,1,'sidecar:movie.srt','字幕','hash')")
            db.execute("INSERT INTO subtitle_cues VALUES(1,0,1000,'原台词')")
            db.execute("INSERT INTO subtitle_corrections VALUES(1,0,1000,'原台词','纠正台词')")
            db.execute("INSERT INTO scripts VALUES(1,1,'剧本','manual:script')")
            db.execute("INSERT INTO script_lines VALUES(1,1,'角色甲','纠正台词',0,1000)")
        self.run = self.root / "runs" / "old"
        self.run.mkdir(parents=True)
        self.detector = self.base / "定制检测器.pt"
        self.detector.write_bytes(b"custom detector checkpoint")
        digest = hashlib.sha256(self.detector.read_bytes()).hexdigest()
        self.digest = digest
        self.write(self.run / "movie_corpus.json", {"weights_sha256": digest})
        self.write(self.run / "movie_sources.json", {"movies": [{"source_path": str(self.source)}]})
        self.write(self.run / "completed.json", {"kind": "movie-temporal", "sample_fps": 2., "patience": 7, "requested_epochs": 40, "initial_learning_rate": .002})
        self.write(self.run / "job.json", {"weights_path": str(self.detector), "epochs": 40, "sample_fps": 2., "patience": 7, "learning_rate": .002})
        (self.run / "movie_temporal.pt").write_bytes(b"temporal checkpoint fixture")
        (self.run / "movie_corpus.npz").write_bytes(b"training corpus fixture")
        OnlineJobManager(self.root).close()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def round_trip(self, **kwargs):
        result = portable.export_project(self.root, self.archive, **kwargs)
        imported = portable.import_project(self.archive, self.target)
        return result, imported

    def test_transfer_preserves_labels_sources_history_and_detector(self):
        from movie_sequence_data import snapshot_sources, _check_source
        history = {path.relative_to(self.run): path.read_bytes() for path in self.run.iterdir()}
        result, imported = self.round_trip()
        self.assertTrue(result["can_continue_movie_learning"])
        self.assertEqual(result["missing_weights"], [])
        self.assertTrue(result["can_resume_latest_model"])
        self.assertTrue(imported["can_resume_latest_model"])
        self.assertEqual(result["movies"], 1)
        self.assertEqual(imported["project"], str(self.target))
        snapshot = snapshot_sources(self.target)
        movie = snapshot["movies"][0]
        moved = _check_source(movie)
        self.assertTrue(moved.is_relative_to(self.target))
        self.assertEqual(moved.read_bytes(), self.source.read_bytes())
        self.assertEqual(movie["subtitles"][0]["text"], "纠正台词")
        self.assertEqual(movie["scripts"][0]["role"], "角色甲")
        self.assertEqual(movie["frames"][0]["boxes"], "[[0,0,0,1,1]]")
        for relative, contents in history.items():
            self.assertEqual((self.target / "runs" / "old" / relative).read_bytes(), contents)
        settings = json.loads((self.target / "portable_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["latest_movie_detector"], f"weights/{self.digest}.pt")
        self.assertEqual((self.target / settings["latest_movie_detector"]).read_bytes(), self.detector.read_bytes())
        self.assertEqual(settings["latest_movie_settings"], {"sample_fps": 2., "epochs": 40, "patience": 7, "learning_rate": .002})
        self.assertEqual(cache_storage.get_settings(self.target)["current"], str(self.target / "online_media"))
        self.assertEqual(cache_storage.get_settings(self.target)["history"], [])

    def test_external_cache_and_online_queue_relocated_and_paused(self):
        external = self.base / "external-cache"
        folder = external / ("a" * 64)
        media = folder / "attempt" / "movie.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"cache fixture")
        thumbnail = folder / "preview.jpg"
        thumbnail.write_bytes(b"preview")
        value = media.stat()
        self.write(folder / "completed.json", {"relative_path": "attempt/movie.mp4", "size": value.st_size, "mtime_ns": value.st_mtime_ns})
        self.write(self.root / cache_storage.CONFIG, {"current": str(external), "history": []})
        with dataset.connect(self.root) as db:
            db.execute("UPDATE movies SET source_path=?,source_size=?,source_mtime=? WHERE id=1", (str(media), value.st_size, value.st_mtime_ns))
        job = {"id": "one", "status": "queued", "result": {"movie_id": 1, "path": str(media), "thumbnail_path": str(thumbnail)}}
        with closing(sqlite3.connect(self.root / "online_jobs.sqlite")) as db, db:
            db.execute("INSERT INTO online_jobs VALUES('one','fingerprint','queued','now','now',?)", (json.dumps(job),))
        self.round_trip()
        moved = self.target / "online_media" / folder.name / "attempt" / "movie.mp4"
        self.assertEqual(moved.read_bytes(), media.read_bytes())
        with closing(sqlite3.connect(self.target / "online_jobs.sqlite")) as db:
            saved = json.loads(db.execute("SELECT data FROM online_jobs WHERE id='one'").fetchone()[0])
            self.assertEqual(db.execute("SELECT value FROM online_meta WHERE key='paused'").fetchone()[0], "1")
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["result"]["path"], str(moved))
        self.assertEqual(saved["result"]["thumbnail_path"], str(moved.parent.parent / "preview.jpg"))
        marker = json.loads((moved.parent.parent / "completed.json").read_text())
        self.assertEqual(marker["mtime_ns"], moved.stat().st_mtime_ns)

    def test_without_videos_is_explicit_and_history_remains_complete(self):
        result, _ = self.round_trip(include_videos=False)
        self.assertFalse(result["can_continue_movie_learning"])
        self.assertFalse(result["can_resume_latest_model"])
        self.assertEqual(result["missing_videos"], [{"id": 1, "name": "影片"}])
        self.assertIsNone(dataset.movies(self.target)[0]["source_path"])
        self.assertTrue((self.target / "runs" / "old" / "movie_temporal.pt").is_file())
        self.assertFalse((self.target / "portable_media").exists())

    def test_missing_detector_is_reported_instead_of_claimed_portable(self):
        self.detector.unlink()
        result, _ = self.round_trip()
        self.assertFalse(result["can_resume_latest_model"])
        self.assertEqual(result["missing_weights"], [{"sha256": self.digest, "runs": ["old"]}])
        settings = json.loads((self.target / "portable_settings.json").read_text())
        self.assertIsNone(settings["latest_movie_detector"])

    def test_internal_custom_cache_and_rounded_timestamps_are_reusable(self):
        from movie_sequence_data import snapshot_sources, _check_source
        base = self.root / "custom-cache"
        folder = base / ("b" * 64)
        folder.mkdir(parents=True)
        media = folder / "video.mp4"
        media.write_bytes(b"internal cached video")
        self.write(folder / "completed.json", {"relative_path": "video.mp4", "size": 0, "mtime_ns": 0})
        self.write(folder / "pending.json", {"folder": str(folder), "path": str(media)})
        self.write(self.root / cache_storage.CONFIG, {"current": str(base), "history": []})
        with dataset.connect(self.root) as db:
            value = media.stat()
            db.execute("UPDATE movies SET source_path=?,source_size=?,source_mtime=?", (str(media), value.st_size, value.st_mtime_ns))
        portable.export_project(self.root, self.archive)
        original_utime = os.utime
        def rounded(path, *, ns):
            return original_utime(path, ns=tuple(value // 2_000_000_000 * 2_000_000_000 for value in ns))
        with patch.object(portable.os, "utime", rounded):
            portable.import_project(self.archive, self.target)
        moved = self.target / "online_media" / folder.name / "video.mp4"
        self.assertEqual(_check_source(snapshot_sources(self.target)["movies"][0]), moved)
        self.assertFalse((self.target / "custom-cache").exists())
        marker = json.loads((moved.parent / "completed.json").read_text())
        self.assertEqual(marker["mtime_ns"], moved.stat().st_mtime_ns)
        pending = json.loads((moved.parent / "pending.json").read_text(encoding="utf-8"))
        self.assertEqual(pending["folder"], str(moved.parent))
        self.assertEqual(pending["path"], str(moved))
        cache_storage._check_owner(self.target, self.target / "online_media")

    def test_live_sqlite_wal_is_backed_up(self):
        with closing(sqlite3.connect(self.root / "annotations.sqlite")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO movie_cast VALUES(1,'角色乙',1)")
            db.commit()
            self.assertTrue((self.root / "annotations.sqlite-wal").exists())
            self.round_trip()
        with dataset.connect(self.target) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT name FROM movie_cast ORDER BY position")], ["角色甲", "角色乙"])

    def test_busy_worker_and_locked_queue_exports_are_blocked(self):
        with self.assertRaisesRegex(ValueError, "仍在执行"):
            portable.export_project(self.root, self.archive, is_busy=lambda: True)
        with patch.object(cache_storage, "active_project_workers", return_value=[{"pid": 123, "task": "train-movie"}]):
            with self.assertRaisesRegex(ValueError, "后台任务"):
                portable.export_project(self.root, self.archive)
        lock = _ProjectLock(self.root / ".online_queue.lock")
        try:
            with self.assertRaisesRegex(ValueError, "队列仍在运行"):
                portable.export_project(self.root, self.archive)
        finally:
            lock.close()
        self.assertFalse(self.archive.exists())

    def test_discovery_history_moves_without_network_or_automatic_handoff(self):
        from discovery_learning import DiscoveryManager
        search = lambda query, **kwargs: [{"title": "公开动画候选", "url": "https://www.bilibili.com/video/BV1xx411c7m0/",
                                           "source_url": "https://www.bing.com/search?q=fixture"}]
        manager = DiscoveryManager(self.root, searcher=search)
        self.addCleanup(manager.close)
        round_id = manager.start({"movie_enabled": False})
        manager._thread.join(4)
        self.assertFalse(manager.busy)
        pending = manager.pending_candidates()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "dispatching")
        self.round_trip()
        with patch("discovery_sources.discover", side_effect=AssertionError("migration must not search")):
            restored = DiscoveryManager(self.target)
            self.addCleanup(restored.close)
            self.assertFalse(restored.busy)
            self.assertFalse(restored.pending_candidates())
            row = restored.list_candidates()[0]
            self.assertEqual(row["state"], "discovered")
            self.assertEqual(row["url"], pending[0]["url"])
            self.assertEqual(restored.list_rounds()[0]["id"], round_id)
            self.assertTrue(restored.list_events())
            restored.request_download([row["id"]])
            self.assertEqual(len(restored.pending_candidates()), 1)

    def test_discovery_search_lock_blocks_export(self):
        folder = self.root/"discovery"
        folder.mkdir()
        lock = _ProjectLock(folder/".worker.lock")
        try:
            with self.assertRaisesRegex(ValueError, "联网发现"):
                portable.export_project(self.root, self.archive)
        finally:
            lock.close()
        self.assertFalse(self.archive.exists())

    def test_changed_resource_during_export_is_rejected(self):
        changed = False
        def mutate(_):
            nonlocal changed
            if not changed:
                self.frame.write_bytes(b"changed frame")
                changed = True
        with self.assertRaisesRegex(ValueError, "发生变化"):
            portable.export_project(self.root, self.archive, report=mutate)
        self.assertFalse(self.archive.exists())

    def test_import_rejects_overwrite_and_checksum_failure_without_partial_project(self):
        portable.export_project(self.root, self.archive)
        self.target.mkdir()
        (self.target / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "全新或空"):
            portable.import_project(self.archive, self.target)
        self.assertEqual((self.target / "keep.txt").read_text(), "keep")
        corrupt = self.base / "corrupt.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(corrupt, "w") as output:
            for name in source.namelist():
                data = source.read(name)
                if name == "project/frames/one.jpg":
                    data = b"x" * len(data)
                output.writestr(name, data)
        empty = self.base / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "校验失败"):
            portable.import_project(corrupt, empty)
        self.assertEqual(list(empty.iterdir()), [])
        new = self.base / "absent"
        with self.assertRaises(ValueError):
            portable.import_project(corrupt, new)
        self.assertFalse(new.exists())
        self.assertEqual(list(self.base.glob(".aecpp-import-*")), [])

    def test_unsafe_archives_are_rejected_before_writing_any_project(self):
        portable.export_project(self.root, self.archive)
        for index, name in enumerate(("../escape", "C:/escape", "/escape", "project/../escape", "project/a\\b", "project/CON")):
            with self.subTest(path=name):
                unsafe = self.base / f"unsafe-{index}.zip"
                with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(unsafe, "w") as output:
                    for original in source.namelist():
                        output.writestr(original, source.read(original))
                    output.writestr(name, b"bad")
                with self.assertRaises(ValueError):
                    portable.import_project(unsafe, self.target)
                self.assertFalse(self.target.exists())
        unsafe = self.base / "symlink.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(unsafe, "w") as output:
            for name in source.namelist():
                output.writestr(name, source.read(name))
            link = zipfile.ZipInfo("project/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, b"../../escape")
        with self.assertRaises(ValueError):
            portable.import_project(unsafe, self.target)
        self.assertFalse(self.target.exists())

    def test_export_rejects_linked_source_tree(self):
        link = self.root / "linked"
        try:
            link.symlink_to(self.base / "outside", target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not permitted on this host")
        with self.assertRaises(ValueError):
            portable.export_project(self.root, self.archive)

    def test_real_model_warm_starts_after_project_transfer(self):
        import numpy as np
        import movie_sequence_data
        import movie_sequence_model as learning
        phases = np.arange(144, dtype=np.float32) / 12
        features = np.column_stack((np.sin(phases), np.cos(phases))).astype(np.float32)
        sequences = [{"offset": 0, "length": 96, "split": "train", "film_id": "a", "movie_id": 1, "name": "A", "start_ms": 0, "end_ms": 96000},
                     {"offset": 96, "length": 48, "split": "val", "film_id": "b", "movie_id": 2, "name": "B", "start_ms": 0, "end_ms": 48000}]
        def prepare(root, snapshot, run, sample_fps, report, **kwargs):
            self.write(run / "movie_corpus.json", {"version": 1, "extractor_version": 1, "feature_names": ["luma", "motion"], "sequences": sequences})
            np.savez(run / "movie_corpus.npz", features=features, times_ms=np.arange(144) * 1000)
        first = self.root / "runs" / "trained"
        with patch.object(movie_sequence_data, "prepare_corpus", prepare):
            learning.train_movie(self.root, movie_sequence_data.snapshot_sources(self.root), 1, "cpu", lambda _: None, run=first)
            original = (first / "movie_temporal.pt").read_bytes()
            self.round_trip()
            snapshot = movie_sequence_data.snapshot_sources(self.target)
            movie_sequence_data._check_source(snapshot["movies"][0])
            following = self.target / "runs" / "continued"
            learning.train_movie(self.target, snapshot, 1, "cpu", lambda _: None, run=following, previous=self.target / "runs" / "trained")
        result = json.loads((following / "completed.json").read_text(encoding="utf-8"))
        self.assertTrue(result["warm_start"])
        self.assertEqual((self.target / "runs" / "trained" / "movie_temporal.pt").read_bytes(), original)
        self.assertTrue((following / "movie_temporal.pt").is_file())


if __name__ == "__main__":
    unittest.main()
