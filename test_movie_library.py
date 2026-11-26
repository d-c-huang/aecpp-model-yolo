"""Library tests use temporary datasets and synthetic files; no media decoder."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import dataset
from movie_library import MovieLibrary, read_library, source_status, visible_movies
import theme


class LibraryFixture:
    def prepare_dataset(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yingge-library-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"project"
        dataset.initialize(self.root)
        self.media = Path(self.temp.name)/"local.mp4"
        self.media.write_bytes(b"synthetic-media-no-decoder")
        stat = self.media.stat()
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path,source_size,source_mtime) VALUES(1,'a','本地原名','time',?,?,?)",
                       (str(self.media),stat.st_size,stat.st_mtime_ns))
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path) VALUES(2,'b','在线原名','val',?)",
                       (str(self.root/'missing.mp4'),))
            db.execute("CREATE TABLE movie_profiles(movie INTEGER PRIMARY KEY,data TEXT,updated_at REAL)")
            db.execute("INSERT INTO movie_profiles VALUES(1,?,0)",
                       (json.dumps({"title":"本地影片","year":"2025","status":"confirmed"}),))
            db.execute("INSERT INTO movie_profiles VALUES(2,?,0)",
                       (json.dumps({"title":"在线影片","status":"manual-needed"}),))
            db.executemany("INSERT INTO frames(movie,slot,ms,path,split,state,deleted) VALUES(1,?,?,?,'train',?,?)",
                           [(1,1000,'one.png','reviewed',0),(2,2000,'two.png','new',0),(3,3000,'deleted.png','reviewed',1)])
        with closing(sqlite3.connect(self.root/'online_jobs.sqlite')) as db:
            db.execute("CREATE TABLE online_jobs(created REAL,data TEXT)")
            db.execute("INSERT INTO online_jobs VALUES(0,?)",
                       (json.dumps({"url":"https://example.test/video","result":{"movie_id":2}}),))
            db.commit()


class LibraryDataTests(LibraryFixture,unittest.TestCase):
    def setUp(self):
        self.prepare_dataset()

    def test_reads_profiles_online_source_and_only_live_frame_counts(self):
        with patch("media.open_movie",side_effect=AssertionError("must not decode")):
            movies = {movie["id"]:movie for movie in read_library(self.root)}
        self.assertEqual(movies[1]["title"],"本地影片")
        self.assertEqual(movies[1]["profile_status"],"confirmed")
        self.assertEqual((movies[1]["frame_count"],movies[1]["reviewed_count"]),(2,1))
        self.assertTrue(movies[1]["source_available"])
        self.assertEqual(movies[2]["source_kind"],"在线视频")
        self.assertEqual(movies[2]["source_host"],"example.test")
        self.assertFalse(movies[2]["source_available"])
        self.assertEqual(movies[2]["mode"],"val")

    def test_missing_or_invalid_profiles_fall_back_without_mutation(self):
        with dataset.connect(self.root) as db:
            db.execute("UPDATE movie_profiles SET data='invalid'")
        movies = read_library(self.root)
        self.assertEqual(movies[0]["title"],"在线原名")
        self.assertEqual(movies[0]["profile_status"],"draft")
        with dataset.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT data FROM movie_profiles LIMIT 1").fetchone()[0],"invalid")
        nonexistent = self.root/'not-created'
        self.assertEqual(read_library(nonexistent),[])
        self.assertFalse(nonexistent.exists())

    def test_source_replacement_is_detected_and_history_is_separate_from_import(self):
        movies = read_library(self.root)
        self.assertEqual([movie["id"] for movie in visible_movies(movies,{},state="已学影片")],[])
        usage = {1:{"completed":2}}
        self.assertEqual([movie["id"] for movie in visible_movies(movies,usage,state="已学影片")],[1])
        self.assertEqual([movie["id"] for movie in visible_movies(movies,usage,state="未学影片")],[2])
        self.media.write_bytes(b"changed")
        self.assertEqual(source_status(next(movie for movie in movies if movie["id"]==1)),(False,"原片已变化"))

    def test_search_and_review_availability_filters(self):
        movies = read_library(self.root)
        for query in ("EXAMPLE.TEST","在线影片"):
            self.assertEqual([movie["id"] for movie in visible_movies(movies,{},query)],[2])
        for state in ("待核对资料","原片不可用"):
            self.assertEqual([movie["id"] for movie in visible_movies(movies,{},state=state)],[2])


class LibraryWindowTests(LibraryFixture,unittest.TestCase):
    def setUp(self):
        self.prepare_dataset()
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.errors = []
        self.tk.report_callback_exception = lambda cls,exc,tb:self.errors.append(str(exc))
        self.callbacks = {name:Mock() for name in ("import","profiles","learn","annotate","subtitles","training","online")}
        self.callbacks["learn"].return_value = True
        self.usage = Mock()
        self.usage.snapshot.return_value = {}
        with patch("movie_library.MovieUsageIndex",return_value=self.usage):
            self.page = MovieLibrary(self.tk,self.root,**{"on_"+name:callback for name,callback in self.callbacks.items()})
        self.page.frame.pack(fill="both",expand=True)
        self.addCleanup(self.cleanup)
        self.wait_for(lambda:len(self.page._movies)==2)

    def wait_for(self,predicate):
        deadline = time.monotonic()+5
        while not predicate() and time.monotonic()<deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertTrue(predicate())
        self.assertFalse(self.errors)

    def cleanup(self):
        self.page.close()
        if self.page._worker:
            self.page._worker.join(2)
        self.tk.destroy()

    def select(self,*ids):
        self.page.tree.selection_set(tuple(str(value) for value in ids))
        self.tk.update()

    def test_multi_selection_callbacks_and_single_movie_actions(self):
        self.select(1,2)
        self.page.action_buttons["profiles"].invoke()
        self.assertEqual(set(self.callbacks["profiles"].call_args.args[0]),{1,2})
        self.assertIn("disabled",self.page.action_buttons["annotate"].state())
        self.select(1)
        self.page.action_buttons["learn"].invoke()
        self.callbacks["learn"].assert_called_once_with([1])
        self.page.action_buttons["annotate"].invoke()
        self.callbacks["annotate"].assert_called_once_with(1)
        self.page.action_buttons["subtitles"].invoke()
        self.callbacks["subtitles"].assert_called_once_with(1)
        self.page.action_buttons["training"].invoke()
        self.callbacks["training"].assert_called_once_with()

    def test_missing_source_does_not_partially_submit_and_still_allows_corrections(self):
        self.select(1,2)
        self.page.action_buttons["learn"].invoke()
        self.callbacks["learn"].assert_not_called()
        self.assertIn("原片不可用",self.page.status.get())
        self.select(2)
        self.page.action_buttons["profiles"].invoke()
        self.callbacks["profiles"].assert_called_once_with([2])
        self.page.action_buttons["annotate"].invoke()
        self.callbacks["annotate"].assert_called_once_with(2)
        self.page.action_buttons["subtitles"].invoke()
        self.callbacks["subtitles"].assert_called_once_with(2)

    def test_source_is_checked_again_immediately_before_learning(self):
        self.select(1)
        self.media.unlink()
        self.page.action_buttons["learn"].invoke()
        self.callbacks["learn"].assert_not_called()
        self.assertIn("重新导入",self.page.status.get())

    def test_filter_preserves_only_visible_selection_and_empty_state(self):
        self.page._select_all()
        self.assertEqual(set(self.page.tree.selection()),{"1","2"})
        self.page.search.set("在线")
        self.assertEqual(self.page.tree.get_children(),("2",))
        self.assertEqual(self.page.tree.selection(),("2",))
        self.page.search.set("没有匹配")
        self.assertFalse(self.page.tree.get_children())
        self.assertIn("disabled",self.page.action_buttons["learn"].state())

    def test_history_refresh_updates_learned_count_without_resetting_selection(self):
        self.select(1)
        self.page.usage = {1:{"completed":3,"last_state":"completed"}}
        self.page._render()
        self.assertEqual(self.page.tree.item("1","values")[4],"已学 3 批")
        self.assertEqual(self.page.tree.selection(),("1",))
        self.assertIn("已学影片 1",self.page.summary.get())

    def test_minimum_layout_keeps_table_and_every_action_visible_with_long_messages(self):
        self.tk.geometry("900x600+0+0")
        self.tk.deiconify()
        self.page.details.set("原片："+str(self.root/('很长的影片路径'*50)))
        self.page.status.set("无法开始学习："+"很长的影片名称与缓存路径/"*80)
        self.tk.update()
        self.assertGreater(self.page.tree.winfo_height(),70)
        body = self.page.frame
        left,top = body.winfo_rootx(),body.winfo_rooty()
        right,bottom = left+body.winfo_width(),top+body.winfo_height()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        buttons = [widget for widget in descendants(body) if widget.winfo_class()=="TButton"]
        self.assertEqual(len(buttons),8)
        for button in buttons:
            with self.subTest(button=button.cget("text")):
                self.assertTrue(button.winfo_ismapped())
                self.assertGreater(button.winfo_width(),1)
                self.assertGreaterEqual(button.winfo_rootx(),left)
                self.assertGreaterEqual(button.winfo_rooty(),top)
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),right)
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),bottom)
        self.assertLessEqual(self.page.status_text.winfo_rooty()+self.page.status_text.winfo_height(),bottom)
        self.assertFalse(self.errors)

    def test_close_cancels_refresh_and_keeps_parent_alive(self):
        self.page.close()
        self.page.refresh()
        self.assertTrue(self.tk.winfo_exists())
        self.assertFalse(self.page.frame.winfo_exists())
        self.assertIsNone(self.page._timer)
        self.usage.close.assert_called_once_with()

    def test_refresh_runs_database_read_off_the_ui_thread(self):
        self.page._worker.join(2)
        entered,release = threading.Event(),threading.Event()
        thread_ids = []
        def slow_read(root):
            thread_ids.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return []
        with patch("movie_library.read_library",side_effect=slow_read):
            self.page.refresh()
            try:
                self.assertTrue(entered.wait(2))
                self.assertNotEqual(thread_ids[0],threading.get_ident())
                self.tk.update()
                self.page.close()
                self.assertTrue(self.tk.winfo_exists())
            finally:
                release.set()
                self.page._worker.join(2)


if __name__=="__main__":
    unittest.main()
