"""Automatic reference jobs travel with projects and exclude destructive cache work."""
import json
from pathlib import Path
import tempfile
import unittest

import cache_storage
import dataset
from auto_learning import AutoLearningManager, build_reference, load_reference
from online_jobs import _ProjectLock
from portable_project import export_project, import_project


class AutoLearningMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base/"source"
        dataset.initialize(self.root)
        self.video = self.root/"fixture.mp4"
        self.video.write_bytes(b"portable fixture: never decoded")
        info = self.video.stat()
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path,source_size,source_mtime) "
                       "VALUES(1,'fixture','迁移参考','train',?,?,?)",
                       (str(self.video), info.st_size, info.st_mtime_ns))
        self.manager = AutoLearningManager(self.root)
        self.addCleanup(self.manager.close)
        self.manager.pause()
        self.job_id = self.manager.enqueue([1])[0]

    def test_queue_drafts_and_trained_reference_survive_move(self):
        board_path = "auto_learning/boards/fixture.json"
        target = self.root/board_path
        target.parent.mkdir(parents=True)
        target.write_text('{"fixture":"portable draft"}', encoding="utf-8")
        with self.manager._db(write=True) as db:
            job = self.manager._get(db, self.job_id)
            job["board_paths"] = [board_path]
            self.manager._save(db, job)
        analysis = {"version": 1, "movie_id": 1, "film_hash": "fixture", "mode": "train",
                    "timing_examples": [{"text": text, "duration": duration, "start_ms": i*5000,
                                         "end_ms": i*5000+int(duration*1000)}
                                        for i, (text, duration) in enumerate([
                                            ("今天要早点睡觉", 2), ("你怎么还没有休息", 2.5), ("明天一定早点睡", 2)])]}
        reference = build_reference([analysis])
        (self.root/"auto_learning/reference.json").write_text(json.dumps(reference), encoding="utf-8")
        archive = self.base/"project.zip"
        export_project(self.root, archive)
        restored_root = self.base/"restored"
        import_project(archive, restored_root)
        restored = AutoLearningManager(restored_root)
        self.addCleanup(restored.close)
        self.assertTrue(restored.paused)
        job = restored.list_jobs()[0]
        self.assertEqual(job["status"], "paused")
        self.assertEqual(Path(job["board_path"]), restored_root/board_path)
        self.assertTrue(Path(job["board_path"]).is_file())
        self.assertEqual(load_reference(restored_root)["coefficients"], reference["coefficients"])
        self.assertFalse(restored.busy)

    def test_cross_instance_lock_blocks_cache_changes_and_project_export(self):
        lock = _ProjectLock(self.root/"auto_learning/.worker.lock")
        self.assertIsNotNone(lock.fd)
        try:
            with self.assertRaisesRegex(ValueError, "自动学习"):
                cache_storage.change_location(self.root, self.base/"new-cache", migrate=True)
            with self.assertRaisesRegex(ValueError, "自动学习"):
                cache_storage.remove_caches(self.root, [])
            with self.assertRaisesRegex(ValueError, "自动学习"):
                export_project(self.root, self.base/"blocked.zip")
        finally:
            lock.close()
        self.assertTrue(self.video.is_file())
        self.assertFalse((self.base/"blocked.zip").exists())


if __name__ == "__main__":
    unittest.main()
