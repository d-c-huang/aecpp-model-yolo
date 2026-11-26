"""Online ingestion tests use finite local HTTP fixtures, never a remote account."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import dataset
import online_video as online


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg builds the finite HTTP fixtures")
class OnlineVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="yingge-online-tests-")
        cls.folder = Path(cls.temporary.name)
        cls.media = cls.folder/"server"
        cls.media.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=8:duration=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
                        "-c:v", "libx264", "-g", "8", "-c:a", "aac", str(cls.media/"clip.mp4")],
                       check=True,capture_output=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(cls.media/"clip.mp4"), "-c", "copy",
                        "-hls_time", "1", "-hls_playlist_type", "vod", "-hls_segment_filename",
                        str(cls.media/"seg%03d.ts"), str(cls.media/"movie.m3u8")],check=True,capture_output=True)
        (cls.media/"index.html").write_text('<html><head><title>测试网页影片</title></head><body>'
                                             '<video controls src="clip.mp4"></video></body></html>',encoding="utf-8")
        live = (cls.media/"movie.m3u8").read_text(encoding="utf-8").replace("#EXT-X-ENDLIST", "")
        (cls.media/"live.m3u8").write_text(live, encoding="utf-8")
        cls.server = ThreadingHTTPServer(("127.0.0.1",0),functools.partial(QuietHandler,directory=str(cls.media)))
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.folder, prefix="project-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        dataset.initialize(self.root)
        self.messages = []

    def test_direct_mp4_cache_reuse_and_original_source_identity(self):
        original_id, _ = dataset.import_movie(self.root,self.media/"clip.mp4","val",lambda _:None)
        original = dataset.movies(self.root)[0]
        events, callback_threads = [], []
        def details(event):
            events.append(event)
            callback_threads.append(threading.get_ident())
        result = online.import_online(self.root,self.base+"clip.mp4","train",self.messages.append,height=720,on_progress=details)
        self.assertEqual(result["movie_id"],original_id)
        self.assertFalse(result["reused"])
        self.assertEqual(result["path"],original["source_path"])
        self.assertEqual(dataset.movies(self.root)[0]["mode"],"val")
        self.assertEqual(dataset.movies(self.root)[0]["name"],original["name"])
        self.assertEqual(result["metadata"]["width"],96)
        self.assertEqual(result["metadata"]["height"],64)
        self.assertAlmostEqual(result["metadata"]["duration"],2.,places=1)
        self.assertEqual(result["metadata"]["source_host"],"127.0.0.1")
        self.assertEqual(result["metadata"]["size"],Path(result["path"]).stat().st_size)
        self.assertTrue(result["metadata"]["has_audio"])
        from PIL import Image
        with Image.open(result["thumbnail_path"]) as preview:
            self.assertEqual(preview.size,(96,64))
            self.assertEqual(preview.format,"JPEG")
        self.assertTrue(Path(result["thumbnail_path"]).is_relative_to(self.root/"online_media"))
        downloaded = [event for event in events if event["phase"]=="downloading" and event["downloaded_bytes"] is not None]
        self.assertTrue(downloaded)
        self.assertTrue(any(event["total_bytes"] for event in downloaded))
        self.assertEqual(events[-1]["phase"],"completed")
        self.assertEqual(events[-1]["percent"],100.)
        self.assertEqual(set(callback_threads),{threading.get_ident()})
        with patch.object(online,"_run_download",side_effect=AssertionError("completed cache must be reused")):
            repeated = online.import_online(self.root,self.base+"clip.mp4","train",self.messages.append)
        self.assertTrue(repeated["reused"])
        self.assertEqual(len(dataset.movies(self.root)),1)

    def test_legacy_cache_gets_local_metadata_and_preview_without_downloading(self):
        first = online.import_online(self.root,self.base+"clip.mp4","time",self.messages.append)
        completed = next((self.root/"online_media").glob("*/completed.json"))
        legacy = online._read_json(completed)
        legacy.pop("metadata",None)
        online._write_json(completed,legacy)
        Path(first["thumbnail_path"]).unlink()
        with patch.object(online,"_run_download",side_effect=AssertionError("legacy cache must not redownload")):
            result = online.import_online(self.root,self.base+"clip.mp4","time",self.messages.append)
        self.assertTrue(result["reused"])
        self.assertEqual(result["movie_id"],first["movie_id"])
        self.assertEqual(result["metadata"],first["metadata"])
        self.assertTrue(Path(result["thumbnail_path"]).is_file())
        self.assertEqual(online._read_json(completed)["metadata"],result["metadata"])

    def test_preview_failure_does_not_discard_valid_video_and_can_retry(self):
        with patch.object(online,"_save_preview",side_effect=OSError("preview unavailable")):
            result = online.import_online(self.root,self.base+"clip.mp4","train",self.messages.append)
        self.assertEqual(result["thumbnail_path"],"")
        self.assertEqual(len(dataset.movies(self.root)),1)
        self.assertTrue(any("预览保存失败" in message for message in self.messages))
        with patch.object(online,"_run_download",side_effect=AssertionError("video remains cached")):
            repeated = online.import_online(self.root,self.base+"clip.mp4","train",self.messages.append)
        self.assertTrue(Path(repeated["thumbnail_path"]).is_file())
        self.assertEqual(repeated["movie_id"],result["movie_id"])

    def test_html_page_imports_single_embedded_video(self):
        result = online.import_online(self.root,self.base+"index.html","time",self.messages.append)
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(dataset.movies(self.root)[0]["name"],result["title"])
        self.assertEqual(dataset.movies(self.root)[0]["mode"],"time")
        self.assertEqual(len(dataset.frames(self.root)),0)

    def test_workbench_batch_downloads_keep_annotation_and_feed_once(self):
        import tkinter as tk
        from unittest.mock import Mock
        from app import Studio

        try:
            window = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        window.withdraw()
        errors = []
        window.report_callback_exception = lambda cls, exc, tb: errors.append(str(exc))
        studio = Studio(window)
        def cleanup():
            manager = studio.online_manager
            if manager is not None:
                manager.close()
                deadline = time.monotonic()+10
                while manager.busy and time.monotonic()<deadline:
                    time.sleep(.02)
            if studio.online_window:
                studio.online_window.close()
            for timer in window.tk.call("after", "info"):
                window.after_cancel(timer)
            window.destroy()
        self.addCleanup(cleanup)
        dataset.import_movie(self.root,self.media/"clip.mp4","time",lambda _:None)
        movie = dataset.movies(self.root)[0]
        dataset.generate_screenshots(self.root,movie,1,lambda _:None,at=.5)
        studio.use(self.root)
        window.update()
        original_frame = studio.row["id"]
        studio.learn_imported_movies = Mock(return_value=True)
        studio.import_online()
        workspace = studio.online_window
        studio.show_annotation()
        workspace.links.insert("1.0", self.base+"clip.mp4\n"+self.base+"index.html")
        workspace._add()
        manager = studio.online_manager
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            window.update()
            if not manager.busy and not manager.has_pending and studio.learn_imported_movies.called:
                break
            time.sleep(.02)
        self.assertFalse(manager.busy,manager.list_jobs())
        self.assertEqual([job["status"] for job in manager.list_jobs()],["completed","completed"])
        self.assertTrue(all(Path(job["result"]["thumbnail_path"]).is_file() for job in manager.list_jobs()))
        self.assertFalse(studio.busy)
        self.assertEqual(studio.row["id"],original_frame)
        self.assertEqual(len(dataset.movies(self.root)),1)
        studio.learn_imported_movies.assert_called_once_with([movie["id"]],request_version=0)
        self.assertFalse(errors)

    def test_finite_hls_and_live_manifest_rejection(self):
        result = online.import_online(self.root,self.base+"movie.m3u8","train",self.messages.append)
        self.assertTrue(Path(result["path"]).is_file())
        with self.assertRaisesRegex(ValueError,"直播|尚未结束"):
            online.import_online(self.root,self.base+"live.m3u8","train",self.messages.append)
        self.assertEqual(len(dataset.movies(self.root)),1)

    def test_cancel_before_download_does_not_register_or_complete_and_can_retry(self):
        stop = threading.Event()
        def report(message):
            if message.startswith("开始缓存"):
                stop.set()
        with self.assertRaisesRegex(RuntimeError,"停止"):
            online.import_online(self.root,self.base+"clip.mp4","train",report,stop=stop)
        self.assertEqual(dataset.movies(self.root),[])
        self.assertEqual(list((self.root/"online_media").glob("*/completed.json")),[])
        self.assertTrue(list((self.root/"online_media").glob("*/pending.json")))
        stop.clear()
        result = online.import_online(self.root,self.base+"clip.mp4","train",self.messages.append,stop=stop)
        self.assertEqual(len(dataset.movies(self.root)),1)
        self.assertFalse(result["reused"])

    def test_progress_cancellation_and_callback_errors_stop_worker_before_registration(self):
        real_popen = subprocess.Popen
        processes = []
        def spawn(*args,**kwargs):
            process = real_popen(*args,**kwargs)
            processes.append(process)
            return process
        for cancel in (True,False):
            with self.subTest(cancel=cancel):
                stop = threading.Event()
                seen = []
                def details(event):
                    seen.append(event)
                    if event["phase"]=="downloading" and event["downloaded_bytes"] is not None:
                        if cancel:
                            stop.set()
                        else:
                            raise LookupError("callback failed")
                expected, pattern = (RuntimeError,"停止") if cancel else (LookupError,"callback failed")
                with patch.object(online.subprocess,"Popen",side_effect=spawn):
                    with self.assertRaisesRegex(expected,pattern):
                        online.import_online(self.root,self.base+"clip.mp4?cancel="+str(cancel),"train",
                                             self.messages.append,stop=stop,on_progress=details)
                self.assertTrue(any(event["downloaded_bytes"] is not None for event in seen))
                self.assertEqual(dataset.movies(self.root),[])
                self.assertEqual(list((self.root/"online_media").glob("*/completed.json")),[])
                self.assertTrue(all(process.poll() is not None for process in processes))
                for folder in (self.root/"online_media").iterdir():
                    if folder.is_dir():
                        with online._cache_lock(folder):
                            pass
                from cache_storage import storage_lease
                with storage_lease(self.root):
                    pass

    def test_url_validation_and_metadata_filters(self):
        self.assertEqual(online.validate_url(" https://example.com/movie?a=1#player "),"https://example.com/movie?a=1")
        for value in ("file:///C:/movie.mp4", "javascript:alert(1)", "https://user:password@example.com/x", "http://example.com:999999"):
            with self.assertRaises(ValueError):
                online.validate_url(value)
        for info in ({"_type":"playlist","entries":[{},{}]}, {"is_live":True}, {"has_drm":True}):
            with self.assertRaises(ValueError):
                online._validate_info(info)
        self.assertEqual(online._validate_info({"duration":None})["duration"],None)

    def test_cached_path_cannot_escape_project(self):
        request={"url":"https://example.com/film", "height":720,"subtitles":True}
        folder=self.root/"online_media"/"cache"
        folder.mkdir(parents=True)
        path=self.media/"clip.mp4"
        stat=path.stat()
        online._write_json(folder/"completed.json",dict(source_url=request["url"],height=720,subtitles=True,
                          relative_path=str(path),size=stat.st_size,mtime_ns=stat.st_mtime_ns))
        self.assertIsNone(online._cached(folder,request))

    def test_preview_symlink_cannot_overwrite_cached_media(self):
        from PIL import Image
        folder = self.root/"preview-cache"
        folder.mkdir()
        video = folder/"media.mp4"
        shutil.copyfile(self.media/"clip.mp4",video)
        before = video.read_bytes()
        try:
            (folder/"preview.jpg").symlink_to(video)
        except OSError:
            self.skipTest("host does not permit file symlinks")
        url = self.base+"clip.mp4"
        info = {"path":str(video),"source_url":url,
                "metadata":online._media_metadata(video,url,duration=2,width=96,height=64,has_audio=True)}
        online._cache_media_details(folder,info,self.messages.append,Image.new("RGB",(96,64)))
        self.assertEqual(info["thumbnail_path"],"")
        self.assertEqual(video.read_bytes(),before)

    def test_downloader_options_and_subtitle_retention(self):
        destination=self.root/"fake"
        destination.mkdir()
        captured={}
        events=[]
        class Downloader:
            def __init__(self,options):
                captured.update(options)
            def __enter__(self):
                return self
            def __exit__(self,*args):
                pass
            def extract_info(self,url,download=False):
                return {"id":"site-id","title":"../../网页影片\n标题","duration":2,"protocol":"https"}
            def process_ie_result(self,info,download=True):
                hook = captured["progress_hooks"][0]
                hook({"status":"downloading","filename":"known","downloaded_bytes":100,
                      "total_bytes":400,"speed":25.,"eta":12})
                hook({"status":"downloading","filename":"unknown","downloaded_bytes":120,
                      "speed":float("nan"),"eta":float("inf")})
                hook({"status":"finished","downloaded_bytes":400,"total_bytes":400})
                captured["postprocessor_hooks"][0]({"status":"started"})
                shutil.copyfile(self_test.media/"clip.mp4",destination/"media.mp4")
                (destination/"media.zh.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\n你好\n",encoding="utf-8")
                return info
        self_test=self
        request={"folder":str(destination),"height":720,"subtitles":True,"url":"https://example.com/page"}
        result=online._download_impl(request,self.messages.append,factory=Downloader,on_progress=events.append)
        self.assertEqual(Path(result["path"]),destination/"media.mp4")
        self.assertTrue(captured["writesubtitles"])
        self.assertTrue(captured["writeautomaticsub"])
        self.assertTrue(captured["noplaylist"])
        self.assertNotIn("cookiesfrombrowser",captured)
        self.assertTrue((destination/"media.zh.vtt").exists())
        downloads = [event for event in events if event["downloaded_bytes"] is not None]
        self.assertEqual(downloads[0]["percent"],25.)
        self.assertEqual(downloads[0]["speed"],25.)
        self.assertEqual(downloads[0]["eta"],12)
        self.assertEqual(downloads[1]["downloaded_bytes"],120)
        for key in ("total_bytes","percent","speed","eta"):
            self.assertIsNone(downloads[1][key])
        self.assertEqual(events[-1]["phase"],"merging")
        self.assertIsNone(events[-1]["percent"])
        self.assertEqual(events[-1]["title"],result["title"])
        json.dumps(events,allow_nan=False)

    def test_import_lock_is_released_after_process_exit(self):
        folder=self.root/"locked"
        folder.mkdir()
        script = ("from pathlib import Path\nimport online_video,time,sys\n"
                  "with online_video._cache_lock(Path(sys.argv[1])):\n"
                  " print('locked',flush=True)\n time.sleep(30)\n")
        proc=subprocess.Popen([sys.executable,"-u","-c",script,str(folder)],stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        try:
            self.assertEqual(proc.stdout.readline().strip(),"locked")
            with self.assertRaisesRegex(ValueError,"另一个任务"):
                with online._cache_lock(folder):
                    pass
        finally:
            proc.terminate()
            proc.communicate(timeout=10)
        with online._cache_lock(folder):
            pass

    @unittest.skipUnless(os.name=="nt", "Windows job handles protect this desktop app")
    def test_process_tree_closes_ffmpeg_like_child(self):
        import ctypes
        from ctypes import wintypes
        script = ("import subprocess,sys,time\n"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
                  "print(p.pid,flush=True)\ntime.sleep(30)\n")
        proc=subprocess.Popen([sys.executable,"-u","-c",script],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              text=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        tree=None
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.WaitForSingleObject.restype=wintypes.DWORD
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=None
        try:
            tree=online._ProcessTree(proc)
            child=int(proc.stdout.readline().strip())
            handle=kernel.OpenProcess(0x00100000,False,child)
            self.assertTrue(handle)
            tree.close()
            proc.wait(timeout=10)
            self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
        finally:
            if tree:
                tree.close()
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=10)
            if handle:
                kernel.CloseHandle(handle)


if __name__=="__main__":
    unittest.main()
