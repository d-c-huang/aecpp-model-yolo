"""UI contract checks use temporary movie records and a manager with no workers."""
from copy import deepcopy
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock

import dataset
import online_window as online
import theme


class FakeManager:
    def __init__(self, root):
        self.root = root
        self.jobs = []
        self.paused = False
        self.busy = False
        self.calls = []

    @property
    def has_pending(self):
        return any(job["status"] in ("running", "paused", "queued") for job in self.jobs)

    def list_jobs(self):
        return deepcopy(self.jobs)

    def add(self, text, **config):
        self.calls.append(("add", config))
        outcome = {"added": [], "duplicates": [], "errors": []}
        for line, url in enumerate(text.splitlines(), 1):
            if not url.strip():
                continue
            if not url.startswith("https://"):
                outcome["errors"].append({"line": line, "text": url, "message": "请输入有效链接"})
            elif any(job["url"] == url for job in self.jobs):
                outcome["duplicates"].append(url)
            else:
                identifier = str(len(self.jobs) + 1)
                self.jobs.append({"id": identifier, "url": url, "status": "paused" if self.paused else "queued", "config": config})
                outcome["added"].append(identifier)
        return outcome

    def start(self):
        self.calls.append(("start",))

    def resume(self, **kwargs):
        self.calls.append(("resume", kwargs))
        self.paused = False

    def pause(self):
        self.calls.append(("pause",))
        self.paused = True

    def retry(self, ids, **kwargs):
        self.calls.append(("retry", ids, kwargs))

    def cancel(self, ids):
        self.calls.append(("cancel", ids))

    def remove(self, ids):
        self.calls.append(("remove", ids))
        self.jobs = [job for job in self.jobs if job["id"] not in ids]


class OnlineWindowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="online-window-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        dataset.initialize(self.root)
        self.video = self.root / "sample.mp4"
        self.video.write_bytes(b"local source fixture")
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path) VALUES(1,'test','测试影片','train',?)", (str(self.video),))
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.addCleanup(self.destroy_tk)
        self.manager = FakeManager(self.root)
        self.open_movie, self.learn, self.closed = Mock(), Mock(), Mock()
        self.open_cache, self.open_profiles, self.learning_settings = Mock(), Mock(), Mock()
        self.window = online.OnlineWindow(self.tk, self.manager, on_open_movie=self.open_movie,
                                          on_learn=self.learn, on_open_training=Mock(),
                                          on_close=self.closed, request_version=lambda: 9,
                                          on_cache=self.open_cache,on_profiles=self.open_profiles,
                                          on_learning_settings=self.learning_settings)
        self.addCleanup(self.window.close)

    def destroy_tk(self):
        try:
            self.tk.destroy()
        except tk.TclError:
            pass  # A lifecycle test may already have destroyed the root.

    def completed(self, identifier="done", **kwargs):
        return {"id": identifier, "url": "https://example.test/movie.mp4", "title": "测试影片", "status": "completed",
                "result": {"movie_id": 1, "path": str(self.video), "metadata": {"duration": 90, "width": 1280, "height": 720, "size": 123456}}, **kwargs}

    def select(self, *ids):
        self.window._refresh_now()
        self.window.tree.selection_set(ids)
        self.window.tree.focus(ids[0])
        self.window._selection_changed()

    def embed(self):
        self.window.close()
        self.closed.reset_mock()
        self.tk.title("电影学习工作台")
        self.tk.geometry("900x700")
        self.tk.deiconify()
        self.container = tk.Frame(self.tk)
        self.container.pack(fill="both", expand=True)
        self.window = online.OnlineWindow(self.tk, self.manager, container=self.container,
                                          on_open_movie=self.open_movie, on_learn=self.learn,
                                          on_open_training=Mock(), on_close=self.closed)
        self.window.win.pack(fill="both", expand=True)
        self.addCleanup(self.window.close)
        self.tk.update()

    def test_add_reports_invalid_lines_and_preserves_pause(self):
        self.manager.paused = True
        self.window.links.insert("1.0", "https://example.test/movie\ninvalid\nhttps://example.test/movie")
        self.window._add()
        self.assertEqual(self.window.links.get("1.0", "end-1c"), "invalid")
        self.assertIn("跳过重复 1", self.window.input_result.cget("text"))
        self.assertFalse(any(call[0] == "start" for call in self.manager.calls))
        self.assertEqual(self.manager.calls[0][1]["request_version"], 9)

    def test_add_starts_queue_and_keeps_errors_editable(self):
        self.window.links.insert("1.0", "https://example.test/movie")
        self.window._add()
        self.assertIn(("start",), self.manager.calls)
        self.assertEqual(self.window.tree.selection(), ("1",))

    def test_resume_and_retry_refresh_training_request_version(self):
        self.manager.paused = True
        self.window._toggle_pause()
        self.assertIn(("resume", {"request_version": 9}), self.manager.calls)
        self.manager.jobs = [self.completed(status="failed")]
        self.select("done")
        self.window._retry()
        self.assertIn(("retry", ["done"], {"request_version": 9}), self.manager.calls)

    def test_feed_only_completed_existing_sources_and_deduplicates(self):
        self.manager.jobs = [self.completed(), self.completed("alias"), self.completed("pending", status="running"),
                             {"id": "gone", "status": "completed", "result": {"movie_id": 99}}]
        self.select("done", "alias", "pending", "gone")
        self.window._feed()
        self.learn.assert_called_once_with([1])
        self.video.unlink()
        self.window._feed()
        self.learn.assert_called_once()

    def test_open_movie_rechecks_record_and_rejected_callback_keeps_window(self):
        self.manager.jobs = [self.completed()]
        self.select("done")
        self.open_movie.return_value = False
        self.window._open_movie()
        self.assertFalse(self.window._closed)
        with dataset.connect(self.root) as db:
            db.execute("DELETE FROM movies WHERE id=1")
        self.window._open_movie()
        self.open_movie.assert_called_once_with(1)

    def test_standalone_open_movie_success_closes_only_online_window(self):
        self.manager.jobs = [self.completed()]
        self.select("done")
        self.window._open_movie()
        self.open_movie.assert_called_once_with(1)
        self.assertTrue(self.window._closed)
        self.assertTrue(self.tk.winfo_exists())
        self.closed.assert_called_once_with()

    def test_search_filter_and_remove_preserve_local_media(self):
        self.manager.jobs = [self.completed(), self.completed("running", status="running", title="排队视频")]
        self.select("done", "running")
        self.window._remove()
        self.assertIn(("remove", ["done"]), self.manager.calls)
        self.assertTrue(self.video.exists())
        self.window.search.set("排队")
        self.assertEqual(self.window.tree.get_children(), ("running",))
        self.window.filter.set("已入库")
        self.assertFalse(self.window.tree.get_children())

    def test_local_thumbnail_and_close_leave_manager_running(self):
        from PIL import Image
        preview = self.root / "preview.jpg"
        Image.new("RGB", (128, 72), (80, 100, 120)).save(preview)
        job = self.completed()
        job["result"]["thumbnail_path"] = str(preview)
        self.manager.jobs = [job]
        self.select("done")
        self.tk.update_idletasks()
        self.assertIsNotNone(self.window._preview_photo)
        self.assertTrue(self.window._timer)
        self.manager.busy = True
        self.window.close()
        self.assertIsNone(self.window._timer)
        self.assertTrue(self.manager.busy)
        self.closed.assert_called_once_with()

    def test_minimum_window_has_visible_queue_and_actions(self):
        self.window.win.geometry("900x620")
        self.tk.update()
        for widget in (self.window.tree, self.window.feed_button, self.window.annotate_button, self.window.web_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLess(widget.winfo_rootx(), self.window.win.winfo_rootx() + 900)
            self.assertLess(widget.winfo_rooty() + widget.winfo_height(), self.window.win.winfo_rooty() + 620)
        self.assertGreater(self.window.tree.winfo_height(), 100)
        self.assertGreater(self.window.details_text.winfo_height(), 65)

    def test_unknown_progress_animates_and_stops_on_completion(self):
        self.manager.jobs = [self.completed(status="running", progress=None, phase="merging")]
        self.select("done")
        self.assertEqual(str(self.window.progress.cget("mode")), "indeterminate")
        self.manager.jobs[0]["status"] = "completed"
        self.window._refresh_now()
        self.assertEqual(str(self.window.progress.cget("mode")), "determinate")
        self.assertEqual(self.window.progress.cget("value"), 100)

    def test_parent_destroy_cleans_timers_without_touching_manager(self):
        errors = []
        self.tk.report_callback_exception = lambda *args: errors.append(args)
        self.window.win.destroy()
        self.assertTrue(self.window._closed)
        self.assertIsNone(self.window._timer)
        self.assertIsNone(self.window._sash_timer)
        self.closed.assert_called_once_with()
        self.assertFalse(errors)

    def test_resource_menu_routes_cache_profiles_and_learning_settings(self):
        self.manager.jobs = [self.completed()]
        self.select("done")
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        buttons = [widget for widget in descendants(self.window.win)
                   if widget.winfo_class()=="TMenubutton" and widget.cget("text")=="资源 / 设置"]
        self.assertEqual(len(buttons),1)
        menu=self.window.win.nametowidget(buttons[0].cget("menu"))
        labels={menu.entrycget(index,"label"):index for index in range(menu.index("end")+1)}
        for label in ("视频缓存与存放位置","所选影片资料","电影学习设置"):
            menu.invoke(labels[label])
        self.open_cache.assert_called_once_with()
        self.open_profiles.assert_called_once_with([1])
        self.learning_settings.assert_called_once_with()

    def test_cache_busy_blocks_add_resume_and_retry_but_allows_pause(self):
        self.window.can_download = lambda:False
        self.window.links.insert("1.0","https://example.test/next.mp4")
        self.window._add()
        self.assertEqual(self.manager.calls,[])
        self.assertIn("缓存管理正在执行",self.window.status.get())
        self.manager.paused=True
        self.window._toggle_pause()
        self.manager.jobs=[self.completed(status="failed")]
        self.select("done")
        self.window._retry()
        self.assertEqual(self.manager.calls,[])
        self.manager.paused=False
        self.window._toggle_pause()
        self.assertEqual(self.manager.calls,[("pause",)])

    def test_waiting_for_profile_review_does_not_claim_training_submitted(self):
        self.manager.jobs=[self.completed()]
        self.select("done")
        self.learn.return_value="waiting-profiles"
        self.window._feed()
        self.learn.assert_called_once_with([1])
        self.assertIn("影片资料窗口",self.window.status.get())
        self.assertNotIn("已提交",self.window.status.get())

    def test_embedded_view_preserves_host_window_and_open_movie_keeps_view(self):
        host_close = Mock()
        self.tk.protocol("WM_DELETE_WINDOW", host_close)
        protocol = self.tk.protocol("WM_DELETE_WINDOW")
        escape = self.tk.bind("<Escape>")
        self.embed()
        self.assertEqual(self.tk.title(), "电影学习工作台")
        self.assertEqual((self.tk.winfo_width(), self.tk.winfo_height()), (900, 700))
        self.assertEqual(self.tk.protocol("WM_DELETE_WINDOW"), protocol)
        self.assertEqual(self.tk.bind("<Escape>"), escape)
        self.assertEqual(self.window.win.winfo_toplevel(), self.tk)
        self.manager.jobs = [self.completed()]
        self.select("done")
        self.window._open_movie()
        self.open_movie.assert_called_once_with(1)
        self.assertTrue(self.window.win.winfo_exists())
        self.assertFalse(self.window._closed)
        self.closed.assert_not_called()
        host_close.assert_not_called()

    def test_embedded_hide_show_retains_queue_selection_and_draft(self):
        self.embed()
        self.manager.jobs = [self.completed(), self.completed("working", status="running")]
        self.select("done")
        self.window.links.insert("1.0", "https://example.test/next.mp4")
        self.window.search.set("测试")
        self.manager.busy = True
        self.window.win.pack_forget()
        self.tk.update()
        self.assertFalse(self.window.win.winfo_ismapped())
        self.manager.jobs[1]["progress"] = 42
        self.window._refresh_now()
        self.window.win.pack(fill="both", expand=True)
        self.window.show()
        self.tk.update()
        self.assertTrue(self.window.win.winfo_ismapped())
        self.assertEqual(self.window.tree.selection(), ("done",))
        self.assertEqual(self.window.links.get("1.0", "end-1c"), "https://example.test/next.mp4")
        self.assertEqual(self.window.search.get(), "测试")
        self.assertEqual(self.window._by_id["working"]["progress"], 42)
        self.assertTrue(self.window._timer)
        self.assertTrue(self.manager.busy)
        self.assertEqual(self.manager.calls, [])

    def test_embedded_close_and_parent_destroy_release_only_view(self):
        self.embed()
        self.manager.busy = True
        self.assertTrue(self.tk.bind("<Control-f>"))
        self.window.close()
        self.assertTrue(self.tk.winfo_exists())
        self.assertFalse(self.window.win.winfo_exists())
        self.assertIsNone(self.window._timer)
        self.assertIsNone(self.window._sash_timer)
        self.assertEqual(self.window._shortcut_bindings, [])
        self.assertFalse(self.tk.bind("<Control-f>"))
        self.assertTrue(self.manager.busy)
        self.closed.assert_called_once_with()
        self.window.close()
        self.closed.assert_called_once_with()
        self.embed()
        errors = []
        self.tk.report_callback_exception = lambda *args: errors.append(args)
        self.container.destroy()
        self.tk.update()
        self.assertTrue(self.window._closed)
        self.assertIsNone(self.window._timer)
        self.assertIsNone(self.window._sash_timer)
        self.assertTrue(self.tk.winfo_exists())
        self.assertTrue(self.manager.busy)
        self.assertFalse(errors)

    def test_embedded_shortcuts_are_scoped_and_preserve_host_bindings(self):
        host_search = Mock()
        host_binding = self.tk.bind("<Control-f>", host_search, add="+")
        self.embed()
        self.window.links.insert("1.0", "https://example.test/next.mp4")
        self.tk.focus_force()
        self.tk.event_generate("<Control-Return>")
        self.tk.update()
        self.assertEqual(self.manager.calls, [])
        self.window._set_composer_expanded(True)
        self.tk.update()
        self.window.links.focus_force()
        self.window.links.event_generate("<Control-Return>")
        self.tk.update()
        self.assertEqual(self.manager.calls[0][0], "add")
        self.assertIn(("start",), self.manager.calls)
        self.window.close()
        self.assertIn(host_binding, self.tk.bind("<Control-f>"))
        self.tk.focus_force()
        self.tk.event_generate("<Control-f>")
        self.tk.update()
        host_search.assert_called_once()

    def test_embedded_root_destroy_cancels_callbacks_without_stopping_jobs(self):
        self.embed()
        self.manager.busy = True
        errors = []
        self.tk.report_callback_exception = lambda *args: errors.append(args)
        self.tk.destroy()
        self.assertTrue(self.window._closed)
        self.assertIsNone(self.window._timer)
        self.assertIsNone(self.window._sash_timer)
        self.assertEqual(self.window._shortcut_bindings, [])
        self.assertTrue(self.manager.busy)
        self.assertEqual(self.manager.calls, [])
        self.closed.assert_called_once_with()
        self.assertFalse(errors)

    def test_embedded_900x700_keeps_queue_and_primary_actions_visible(self):
        self.embed()
        for width, height in ((900, 700), (900, 600), (940, 550)):
            with self.subTest(width=width, height=height):
                self.tk.geometry(f"{width}x{height}")
                self.tk.update()
                for widget in (self.window.tree, self.window.feed_button, self.window.annotate_button, self.window.web_button):
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.tk.winfo_rootx() + width)
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.tk.winfo_rooty() + height)
                self.assertGreater(self.window.tree.winfo_height(), 100)
                self.assertGreater(self.window.details_text.winfo_height(), 65)


if __name__ == "__main__":
    unittest.main()
