"""Real Tk checks for the discovery view; no requests or media downloads."""
import copy
import gc
import tkinter as tk
import unittest
from unittest.mock import Mock

import discovery_window as discovery
import theme


class FakeManager:
    def __init__(self):
        self.busy = False
        self.config = {}
        self.data = {"busy": False, "candidates": [], "events": [], "active": {}}
        self.calls = []

    def get_config(self):
        return copy.deepcopy(self.config)

    def snapshot(self):
        return copy.deepcopy({**self.data, "busy": self.busy})

    def start(self, config):
        self.calls.append(("start", config))
        self.busy = True
        return "round-1"

    def stop(self):
        self.calls.append(("stop",))
        self.busy = False


class DiscoveryWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.manager = FakeManager()
        self.open_url, self.open_learning, self.queued, self.closed = Mock(), Mock(), Mock(), Mock()
        self.activity = {"current_downloads": [], "current_learning": []}
        self.window = discovery.DiscoveryWindow(self.tk, self.manager, on_open_url=self.open_url,
            on_open_learning=self.open_learning, on_queue_selected=self.queued, on_close=self.closed,
            activity=lambda: self.activity)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.window.close()
        self.tk.report_callback_exception = None
        try:
            self.tk.destroy()
        except tk.TclError:
            pass
        self.window = self.tk = None
        gc.collect()

    @staticmethod
    def candidate(identifier="video", **kwargs):
        return {"id": identifier, "title": "沙雕动画测试素材", "url": "https://www.bilibili.com/video/BVfixture",
                "source_url": "https://www.bing.com/search?q=site%3Abilibili.com+%E6%B2%99%E9%9B%95",
                "category": "animation", "provider": "web", "site": "bilibili", "state": "discovered",
                "learning_kind": "story-reference", **kwargs}

    def select(self, *ids):
        self.window.refresh_now()
        self.window.tree.selection_set(ids)
        self.window._selection_changed()

    def test_defaults_start_finite_round_with_only_selected_video_sites(self):
        self.assertEqual(self.manager.calls, [])
        self.window.start_button.invoke()
        name, config = self.manager.calls[0]
        self.assertEqual(name, "start")
        self.assertEqual(config["sites"], ["bilibili", "youku", "iqiyi"])
        self.assertEqual((config["animation_provider"], config["movie_provider"]), ("web", "web"))
        self.assertEqual(config["movie_query"], "喜剧电影")
        self.assertTrue(config["animation_enabled"] and config["movie_enabled"] and config["auto_download"])
        self.assertEqual((config["max_per_category"], config["max_total_downloads"]), (3, 4))
        self.assertTrue(self.window.start_button.instate(["disabled"]))
        self.assertTrue(self.window.limit_box.instate(["disabled"]))
        self.open_url.assert_not_called()

    def test_settings_validation_and_project_write_guard(self):
        for value in self.window.sites.values():
            value.set(False)
        self.window._start()
        self.assertIn("至少选择一个视频网站", self.window.status.get())
        self.window.sites["youku"].set(True)
        self.window.max_per_category.set("11")
        self.window._start()
        self.assertIn("1–10", self.window.status.get())
        self.window.max_per_category.set("2")
        self.window.can_write = lambda: False
        self.window._start()
        self.assertEqual(self.manager.calls, [])
        self.assertIn("迁移", self.window.status.get())
        self.window.can_write = lambda: True
        self.window._start()
        self.assertEqual(self.manager.calls[0][1]["sites"], ["youku"])

    def test_actual_search_source_and_download_metrics_are_separate_from_video_site(self):
        source = "https://www.bing.com/search?q=site%3Abilibili.com+%E6%B2%99%E9%9B%95"
        self.manager.busy = True
        self.manager.data["active"] = {"phase": "fetch", "provider": "bing", "site": "bilibili", "query": "沙雕动画", "url": source}
        self.manager.data["candidates"] = [self.candidate(state="downloading", percent=25.5, downloaded_bytes=1024,
                                                       total_bytes=4096, speed=512)]
        self.activity["current_downloads"] = [{"title": "沙雕动画测试素材", "source_url": "https://www.bilibili.com/video/BVfixture",
            "percent": 25.5, "downloaded_bytes": 1024, "total_bytes": 4096, "speed": 512}]
        self.activity["current_learning"] = [{"title": "喜剧电影测试素材", "kind": "story-and-movie", "message": "等待补充影片资料"}]
        self.select("video")
        self.assertIn("bing", self.window.current_source.get())
        self.assertIn("哔哩哔哩", self.window.current_source.get())
        self.assertEqual(self.window.current_url.get(), source)
        self.assertEqual(self.window.tree.item("video", "values")[2], "哔哩哔哩")
        self.assertIn("25.5%", self.window.download_activity.get())
        self.assertIn("512 B/s", self.window.download_activity.get())
        self.assertIn("字幕节奏参考 + 电影时序训练", self.window.learning_activity.get())
        self.assertIn("等待补充影片资料", self.window.learning_activity.get())
        self.open_url.assert_not_called()
        self.window.source_button.invoke()
        self.open_url.assert_called_once_with(source)

    def test_failed_previous_job_is_retryable_and_only_eligible_selection_is_queued(self):
        self.manager.data["candidates"] = [self.candidate("failed", state="failed", detail="下载失败：网络超时", online_job_id="old-job"),
            self.candidate("running", state="downloading"), self.candidate("new")]
        self.select("failed", "running", "new")
        self.assertIn("网络超时", self.window.details.get("1.0", "end"))
        self.window.queue_button.invoke()
        self.queued.assert_called_once_with(["failed", "new"])
        self.window._stop()
        self.assertEqual(self.manager.calls, [("stop",)])
        self.window.learning_button.invoke()
        self.open_learning.assert_called_once_with()

    def test_event_details_survive_poll_and_return_to_material_tab(self):
        self.manager.data["candidates"] = [self.candidate()]
        event = {"id": "e1", "timestamp": "2026-11-27T10:00:00Z", "phase": "fetch", "query": "电影搜索词",
                 "url": "https://www.google.com/search?q=fixture", "message": "实际请求失败：连接超时"}
        self.manager.data["events"] = [event]
        self.select("video")
        self.window.notebook.select(1)
        self.window.log_tree.selection_set("e1")
        self.window._event_selected()
        self.window.refresh_now()
        self.assertIn("实际请求失败：连接超时", self.window.details.get("1.0", "end"))
        self.window.notebook.select(0)
        self.window._tab_changed()
        self.assertIn("沙雕动画测试素材", self.window.details.get("1.0", "end"))
        self.assertEqual(len(self.window.log_tree.get_children()), 1)
        self.assertEqual(self.manager.calls, [])

    def test_movie_detail_and_needs_input_explain_real_learning_content(self):
        self.manager.data["candidates"] = [self.candidate(category="movie", learning_kind="story-and-movie",
            state="needs_input", detail="视频无可用字幕，请补充字幕；电影资料未准备完成")]
        self.select("video")
        text = self.window.details.get("1.0", "end")
        self.assertIn("字幕节奏参考 + 电影时序训练", text)
        self.assertIn("无可用字幕", text)
        self.assertNotIn("学习完成", text)
        self.assertNotIn("100%", self.window.tree.item("video", "values")[-1])

    def test_filter_and_settings_restore(self):
        self.manager.data["candidates"] = [self.candidate(), self.candidate("movie", title="喜剧电影")]
        self.window.refresh_now()
        self.window.search.set("喜剧")
        self.assertEqual(self.window.tree.get_children(), ("movie",))
        self.window.close()
        self.manager.config = {"sites": ["iqiyi"], "movie_query": "电影喜剧", "max_total_downloads": 2, "auto_download": False}
        self.window = discovery.DiscoveryWindow(self.tk, self.manager)
        self.assertEqual(self.window.movie_query.get(), "电影喜剧")
        self.assertEqual(self.window.max_total_downloads.get(), "2")
        self.assertFalse(self.window.auto_download.get())
        self.assertEqual([key for key, value in self.window.sites.items() if value.get()], ["iqiyi"])

    def test_minimum_980x700_keeps_main_actions_and_table_visible(self):
        self.activity["current_downloads"] = [{"title": "测试沙雕动画", "percent": 32.3, "speed": 1024}]
        self.activity["current_learning"] = [{"title": "电影测试", "kind": "story-and-movie", "message": "等待补充资料"}]
        self.window.refresh_now()
        self.window.win.geometry("980x700")
        self.tk.update()
        right = self.window.win.winfo_rootx()+self.window.win.winfo_width()
        bottom = self.window.win.winfo_rooty()+self.window.win.winfo_height()
        for widget in (self.window.start_button, self.window.stop_button, self.window.source_button,
                       self.window.queue_button, self.window.tree, self.window.learning_button, self.window.open_video_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(), right)
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), bottom)
        self.assertGreater(self.window.tree.winfo_height(), 100)

    def test_close_removes_timer_and_trace_without_stopping_search(self):
        self.manager.busy = True
        timer = self.window._timer
        self.assertTrue(timer)
        self.window.close()
        self.assertIsNone(self.window._timer)
        self.assertEqual(self.window.search.trace_info(), [])
        self.window._render_candidates = Mock(side_effect=AssertionError("closed view received trace"))
        self.window.search.set("closed")
        self.window._render_candidates.assert_not_called()
        self.assertTrue(self.manager.busy)
        self.assertEqual(self.manager.calls, [])
        self.closed.assert_called_once_with()
        self.window.close()
        self.closed.assert_called_once_with()

    def test_parent_destroy_cancels_callbacks(self):
        errors = []
        self.tk.report_callback_exception = lambda *args: errors.append(args)
        self.tk.destroy()
        self.assertTrue(self.window._closed)
        self.assertIsNone(self.window._timer)
        self.assertEqual(self.window.search.trace_info(), [])
        self.assertFalse(errors)
        self.assertEqual(self.manager.calls, [])


class DisplayHelpersTests(unittest.TestCase):
    def test_unavailable_progress_stays_unknown(self):
        self.assertEqual(discovery.progress_text({}), "")
        self.assertEqual(discovery.progress_text({"percent": float("nan"), "speed": -1, "eta": float("inf")}), "")
        self.assertEqual(discovery.progress_text({"percent": 0}), "0.0%")

    def test_source_url_hides_credentials_and_rejects_non_browser_schemes(self):
        self.assertEqual(discovery.safe_url("file:///D:/secret"), "")
        self.assertEqual(discovery.safe_url("https://user:secret@www.bilibili.com/"), "")
        shown = discovery.display_url("https://www.bing.com/search?q=movie&token=private#fragment")
        self.assertIn("q=movie", shown)
        self.assertNotIn("private", shown)
        self.assertNotIn("fragment", shown)


if __name__ == "__main__":
    unittest.main()
