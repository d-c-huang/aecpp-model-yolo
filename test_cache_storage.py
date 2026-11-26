"""Cache changes operate only on temporary, managed video fixtures."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import cache_storage as storage
import dataset
from online_jobs import OnlineJobManager


class CacheStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yingge-cache-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/"project"
        dataset.initialize(self.root)
        self.target = self.base/"new-cache"

    def cache(self, name="one", *, complete=True, registered=False):
        key = hashlib.sha256(name.encode()).hexdigest()
        folder = self.root/"online_media"/key
        folder.mkdir(parents=True)
        media = folder/"attempt-test"/"media.mp4"
        media.parent.mkdir()
        media.write_bytes((name*100).encode())
        preview = folder/"preview.jpg"
        preview.write_bytes(b"preview")
        value = media.stat()
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        metadata = {"title":name,"source_url":"https://example.test/"+name,"relative_path":media.relative_to(folder).as_posix(),
                    "size":value.st_size,"mtime_ns":value.st_mtime_ns,"sha256":digest,"height":720,"subtitles":True}
        if complete:
            storage._write(folder/"completed.json",metadata)
        else:
            storage._write(folder/"pending.json",{"attempt":"attempt-test","config":{"url":"https://example.test/"+name}})
        movie_id = None
        if registered:
            with dataset.connect(self.root) as db:
                movie_id = db.execute("INSERT INTO movies(digest,name,mode,source_path,source_size,source_mtime) VALUES(?,?,'train',?,?,?)",
                                      (digest,name,str(media),value.st_size,value.st_mtime_ns)).lastrowid
                db.execute("INSERT INTO frames(movie,slot,ms,path,split) VALUES(?,0,0,?,'train')",(movie_id,name+".png"))
                db.execute("INSERT INTO subtitle_tracks(movie,source,label,fingerprint) VALUES(?,'sidecar:media.zh.srt','字幕','cue')",(movie_id,))
        return folder,media,movie_id

    def test_switch_only_preserves_old_cache_lookup_and_new_destination(self):
        folder,media,_ = self.cache()
        result = storage.change_location(self.root,self.target)
        self.assertEqual(result["migrated"],0)
        self.assertTrue(media.is_file())
        self.assertEqual(storage.locate_cache(self.root,folder.name),folder)
        fresh = "f"*64
        self.assertEqual(storage.locate_cache(self.root,fresh),self.target/fresh)
        self.assertEqual(storage.get_settings(self.root)["current"],str(self.target))

    def test_migration_preserves_movie_annotations_queue_and_frozen_training_paths(self):
        folder,media,movie_id = self.cache(registered=True)
        source_snapshot = self.root/"runs"/"old"/"movie_sources.json"
        source_snapshot.parent.mkdir(parents=True)
        source_snapshot.write_text(json.dumps({"movies":[{"source_path":str(media)}]}),encoding="utf-8")
        manager = OnlineJobManager(self.root)
        manager.close()
        before = source_snapshot.read_bytes()
        result = storage.change_location(self.root,self.target,migrate=True)
        moved = self.target/folder.name/media.relative_to(folder)
        self.assertEqual(result["migrated"],1)
        self.assertFalse(folder.exists())
        self.assertTrue(moved.is_file())
        self.assertEqual(dataset.movies(self.root)[0]["source_path"],str(moved))
        self.assertEqual(dataset.movies(self.root)[0]["source_mtime"],moved.stat().st_mtime_ns)
        self.assertEqual(len(dataset.frames(self.root)),1)
        with dataset.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT source FROM subtitle_tracks").fetchone()[0],"sidecar:media.zh.srt")
        with closing(sqlite3.connect(self.root/"online_jobs.sqlite")) as db:
            row = json.loads(db.execute("SELECT data FROM online_jobs").fetchone()[0])
        self.assertEqual(row["result"]["path"],str(moved))
        self.assertEqual(row["result"]["thumbnail_path"],str(self.target/folder.name/"preview.jpg"))
        self.assertEqual(source_snapshot.read_bytes(),before)

    def test_migration_updates_partial_download_paths(self):
        folder,media,_ = self.cache(complete=False)
        request = media.parent/"request.json"
        storage._write(request,{"folder":str(media.parent),"url":"https://example.test/movie"})
        storage._write(media.parent/"download.json",{"path":str(media)})
        storage.change_location(self.root,self.target,migrate=True)
        updated = storage._read(self.target/folder.name/request.relative_to(folder))
        self.assertEqual(updated["folder"],str(self.target/folder.name/media.parent.relative_to(folder)))

    def test_migration_refreshes_destination_timestamp_precision_without_redownload(self):
        import online_video
        folder,media,_ = self.cache(registered=True)
        original = storage._read(folder/"completed.json")
        original["metadata"] = {"size":original["size"]}
        storage._write(folder/"completed.json",original)
        real_copy = shutil.copy2
        def rounded_copy(source,target):
            result = real_copy(source,target)
            if Path(target).name=="media.mp4":
                value = Path(target).stat()
                os.utime(target,ns=(value.st_atime_ns,value.st_mtime_ns//2_000_000_000*2_000_000_000))
            return result
        # A transient DB error leaves a verified destination copy. Retry must
        # tolerate its corrected metadata without accepting different media.
        with patch.object(storage.shutil,"copy2",side_effect=rounded_copy), \
             patch.object(storage,"_switch_references",side_effect=sqlite3.OperationalError("busy")):
            with self.assertRaises(sqlite3.OperationalError):
                storage.change_location(self.root,self.target,migrate=True)
        storage.change_location(self.root,self.target,migrate=True)
        target_folder = self.target/folder.name
        cached = online_video._cached(target_folder,{"url":original["source_url"],"height":720,"subtitles":True})
        self.assertIsNotNone(cached)
        actual = Path(cached["path"]).stat()
        self.assertEqual(cached["mtime_ns"],actual.st_mtime_ns)
        self.assertEqual(cached["metadata"]["size"],actual.st_size)
        self.assertEqual(dataset.movies(self.root)[0]["source_mtime"],actual.st_mtime_ns)

    def test_migration_rejects_completed_media_path_escape(self):
        folder,media,_ = self.cache()
        value = storage._read(folder/"completed.json")
        value["relative_path"] = "../../outside.mp4"
        storage._write(folder/"completed.json",value)
        with self.assertRaisesRegex(ValueError,"越界"):
            storage.change_location(self.root,self.target,migrate=True)
        self.assertTrue(media.exists())
        self.assertFalse((self.root/storage.CONFIG).exists())

    def test_conflicting_destination_never_overwrites_or_changes_source_settings(self):
        folder,media,_ = self.cache(registered=True)
        conflicting = self.target/folder.name/media.relative_to(folder)
        conflicting.parent.mkdir(parents=True)
        conflicting.write_bytes(b"do not overwrite")
        with self.assertRaisesRegex(ValueError,"不会覆盖"):
            storage.change_location(self.root,self.target,migrate=True)
        self.assertEqual(conflicting.read_bytes(),b"do not overwrite")
        self.assertTrue(media.exists())
        self.assertEqual(storage.get_settings(self.root)["current"],str(self.root/"online_media"))
        self.assertEqual(dataset.movies(self.root)[0]["source_path"],str(media))

    def test_copy_verification_failure_retains_old_source_and_configuration(self):
        folder,media,_ = self.cache(registered=True)
        with patch.object(storage.shutil,"copy2",side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError,"disk failure"):
                storage.change_location(self.root,self.target,migrate=True)
        self.assertTrue(media.exists())
        self.assertFalse((self.root/storage.CONFIG).exists())
        self.assertEqual(dataset.movies(self.root)[0]["source_path"],str(media))
        self.assertFalse(list(self.target.glob(".moving-*")))

    def test_db_update_failure_retains_source(self):
        _,media,_ = self.cache(registered=True)
        with patch.object(storage,"_switch_references",side_effect=sqlite3.OperationalError("database busy")):
            with self.assertRaisesRegex(sqlite3.OperationalError,"database busy"):
                storage.change_location(self.root,self.target,migrate=True)
        self.assertTrue(media.exists())
        self.assertFalse((self.root/storage.CONFIG).exists())
        self.assertEqual(dataset.movies(self.root)[0]["source_path"],str(media))
        # The verified copy can be reused after a transient database failure.
        storage.change_location(self.root,self.target,migrate=True)
        self.assertFalse(media.exists())
        self.assertTrue(Path(dataset.movies(self.root)[0]["source_path"]).is_file())

    def test_referenced_deletion_requires_confirmation_and_preserves_annotations(self):
        folder,media,movie_id = self.cache(registered=True)
        manager = OnlineJobManager(self.root)
        manager.close()
        with self.assertRaisesRegex(ValueError,"原片"):
            storage.remove_caches(self.root,[folder])
        self.assertTrue(media.exists())
        result = storage.remove_caches(self.root,[folder],allow_referenced=True)
        self.assertEqual(result["movie_ids"],[movie_id])
        self.assertFalse(media.exists())
        self.assertEqual(len(dataset.frames(self.root)),1)
        self.assertEqual(len(dataset.movies(self.root)),1)
        with closing(sqlite3.connect(self.root/"online_jobs.sqlite")) as db:
            row = json.loads(db.execute("SELECT data FROM online_jobs").fetchone()[0])
        self.assertEqual(row["status"],"cancelled")
        self.assertEqual(row["result"]["thumbnail_path"],"")
        self.assertFalse(row["learn"])

    def test_cleanup_keeps_referenced_movies_and_unknown_files(self):
        used,media,_ = self.cache("used",registered=True)
        unused,_,_ = self.cache("unused")
        partial,_,_ = self.cache("partial",complete=False)
        unknown = self.root/"online_media"/"personal-folder"
        unknown.mkdir()
        (unknown/"important.txt").write_text("preserve")
        result = storage.cleanup_unused(self.root,incomplete_only=True)
        self.assertEqual(result["removed"],1)
        self.assertFalse(partial.exists())
        self.assertTrue(unused.exists())
        storage.cleanup_unused(self.root)
        self.assertTrue(used.exists())
        self.assertTrue(media.exists())
        self.assertFalse(unused.exists())
        self.assertEqual((unknown/"important.txt").read_text(),"preserve")

    def test_managed_path_bounds_reject_unregistered_and_nested_locations(self):
        folder,media,_ = self.cache()
        for value in (self.base,self.root,self.root/"runs"/"cache",folder/"nested",self.root/"online_media"/"nested"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                storage.change_location(self.root,value)
        with self.assertRaises(ValueError):
            storage.remove_caches(self.root,[self.base])
        outside = self.base/("e"*64)
        outside.mkdir()
        storage._write(outside/"completed.json",{"title":"outside"})
        with self.assertRaises(ValueError):
            storage.remove_caches(self.root,[outside])
        self.assertTrue(media.exists())

    def test_download_lease_blocks_mutating_cache_operations(self):
        folder,_,_ = self.cache()
        with storage.storage_lease(self.root):
            with self.assertRaisesRegex(ValueError,"正在下载或整理"):
                storage.change_location(self.root,self.target)
            with self.assertRaisesRegex(ValueError,"正在下载或整理"):
                storage.remove_caches(self.root,[folder])
        storage.change_location(self.root,self.target)

    def test_another_project_cannot_claim_owned_cache_location(self):
        storage.change_location(self.root,self.target)
        second = self.base/"second"
        dataset.initialize(second)
        with self.assertRaisesRegex(ValueError,"另一个工程"):
            storage.change_location(second,self.target)

    def test_process_guard_matches_only_our_script_and_exact_project(self):
        application = Path(storage.__file__).resolve().parent
        def process(pid,args,working=None):
            return SimpleNamespace(pid=pid,info={"name":"python.exe"},cmdline=lambda:args,
                                   cwd=lambda:str(working or application))
        exact = process(987651,["python","-u",str(application/"model_tasks.py"),"train-movie",str(self.root),""])
        other_project = process(987652,["python",str(application/"model_tasks.py"),"train",str(self.base/"other"),""])
        other_app = process(987653,["python",str(self.base/"model_tasks.py"),"track",str(self.root),""])
        as_module = process(987654,["python","-m","model_tasks","predict",str(self.root),""])
        online = process(987655,["python",str(application/"online_video.py"),"--worker",str(self.root/"online_media"/("f"*64)/"attempt-1"/"request.json")])
        found = storage.active_project_workers(self.root,processes=[exact,other_project,other_app,as_module,online])
        self.assertEqual(found,[{"pid":987651,"task":"train-movie"},{"pid":987654,"task":"predict"},{"pid":987655,"task":"download"}])

    def test_old_running_worker_blocks_migration_and_deletion_without_process_control(self):
        folder,media,_ = self.cache()
        with patch.object(storage,"active_project_workers",return_value=[{"pid":987651,"task":"train-movie"}]):
            with self.assertRaisesRegex(ValueError,"PID 987651"):
                storage.change_location(self.root,self.target,migrate=True)
            with self.assertRaisesRegex(ValueError,"未停止任何任务"):
                storage.remove_caches(self.root,[folder])
        self.assertTrue(media.is_file())
        self.assertFalse(self.target.exists())

    def test_worker_starting_during_migration_keeps_old_source_copy(self):
        folder,media,_ = self.cache(registered=True)
        with patch.object(storage,"active_project_workers",side_effect=[[],[],[{"pid":987651,"task":"train-movie"}]]):
            result = storage.change_location(self.root,self.target,migrate=True)
        self.assertTrue(media.exists())
        self.assertTrue(result["warnings"])
        self.assertTrue(Path(dataset.movies(self.root)[0]["source_path"]).is_file())

    def test_symlink_cannot_escape_managed_tree(self):
        folder,_,_ = self.cache()
        outside = self.base/"important.txt"
        outside.write_text("preserve")
        try:
            (folder/"outside-link").symlink_to(outside)
        except OSError:
            self.skipTest("host does not permit symlinks")
        with self.assertRaises(ValueError):
            storage.remove_caches(self.root,[folder])
        with self.assertRaises(ValueError):
            storage.change_location(self.root,self.target,migrate=True)
        self.assertEqual(outside.read_text(),"preserve")

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg builds local HTTP media")
    def test_real_download_uses_selected_path_and_reuses_migrated_cache(self):
        import online_video
        import test_online_video as fixtures
        fixtures.OnlineVideoTests.setUpClass()
        self.addCleanup(fixtures.OnlineVideoTests.tearDownClass)
        storage.change_location(self.root,self.target)
        result = online_video.import_online(self.root,fixtures.OnlineVideoTests.base+"clip.mp4","time",lambda _:None)
        self.assertTrue(Path(result["path"]).is_relative_to(self.target))
        final = self.base/"final-cache"
        storage.change_location(self.root,final,migrate=True)
        with patch.object(online_video,"_run_download",side_effect=AssertionError("must reuse migrated complete cache")):
            repeated = online_video.import_online(self.root,fixtures.OnlineVideoTests.base+"clip.mp4","time",lambda _:None)
        self.assertTrue(repeated["reused"])
        self.assertEqual(result["movie_id"],repeated["movie_id"])
        self.assertTrue(Path(repeated["path"]).is_relative_to(final))
        self.assertTrue(Path(repeated["thumbnail_path"]).is_file())


if __name__=="__main__":
    unittest.main()
