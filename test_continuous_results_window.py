"""Cached cumulative outcomes and skip explanations, without worker activity."""
from copy import deepcopy
import gc
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import auto_learning_window
import dataset
import discovery_window
import online_window
import test_auto_learning_window as auto_fixtures
import test_discovery_window as discovery_fixtures
import test_online_window as online_fixtures
import theme


def totals(**changes):
    return {"ready": True, "video_count": 3, "completed_count": 2, "partial_count": 1,
            "subtitle_reference_count": 18, "visual_clip_count": 7, "visual_frame_count": 21,
            "skipped_count": 2, "latest_skip": {"site": "iqiyi", "title": "受限影片",
                "reason": "会员或付费内容，已跳过", "url": "https://www.iqiyi.com/v_fixture.html",
                "source_url": "https://search.video.iqiyi.com/search?token=private-token&key=movie",
                "timestamp": "2026-11-27T12:00:00Z"}, **changes}


class ResultsTextTests(unittest.TestCase):
    def test_unknown_zero_and_invalid_counts_remain_distinct(self):
        format_text = online_window.continuous_results_text
        unknown = format_text({"totals": {"ready": False, "video_count": 99, "message": "正在读取成果"}})
        self.assertIn("待统计", unknown["summary"])
        self.assertNotIn("99", unknown["summary"])
        self.assertNotIn("0", unknown["skipped"])
        zero = format_text({"totals": totals(**{key: 0 for key in totals() if key.endswith("_count")}, latest_skip=None)})
        self.assertIn("视频 0 部", zero["summary"])
        self.assertIn("视觉 0 段 / 0 帧", zero["summary"])
        self.assertIn("累计跳过 0 个视频", zero["skipped"])
        invalid = format_text({"totals": totals(video_count=-1, partial_count=True,
                            visual_frame_count=float("nan"), skipped_count=None, latest_skip=None)})
        self.assertIn("视频 — 部", invalid["summary"])
        self.assertIn("部分完成 —", invalid["summary"])
        self.assertIn("视觉 7 段 / — 帧", invalid["summary"])
        self.assertIn("最近原因待获取", invalid["skipped"])

    def test_partial_outcomes_and_recent_skip_sources_are_not_hidden(self):
        values = online_window.continuous_results_text({"totals": totals(recent_skips=[
            {"site": "youku", "reason": "需要登录", "source_url": "https://www.youku.com/ku/webhome"}])})
        self.assertIn("完成 2 / 部分完成 1", values["summary"])
        self.assertIn("字幕节奏 18 条", values["summary"])
        self.assertIn("最近：爱奇艺", values["skipped"])
        self.assertIn("会员或付费", values["skipped"])
        self.assertIn("需要登录", values["details"])
        self.assertIn("search.video.iqiyi.com", values["details"])
        self.assertIn("v_fixture.html", values["details"])
        self.assertNotIn("private-token", str(values))


class ContinuousResultsWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory(prefix="continuous-results-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        dataset.initialize(self.root)
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.windows = []
        self.state = {"state": "paused", "enabled": False, "totals": totals()}
        self.snapshot = Mock(side_effect=lambda: deepcopy(self.state))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for window in self.windows:
            window.close()
        self.windows.clear()
        self.tk.report_callback_exception = None
        try:
            self.tk.destroy()
        except tk.TclError:
            pass
        self.tk = None
        gc.collect()

    def create(self, kind, *, embedded=False):
        common = {"continuous_status": self.snapshot, **({"container": self.tk} if embedded else {})}
        if kind == "online":
            window = online_window.OnlineWindow(self.tk, online_fixtures.FakeManager(self.root),
                on_open_movie=Mock(), on_learn=Mock(), on_open_training=Mock(), on_auto_learning=Mock(),
                on_auto_learn=Mock(), on_discovery=Mock(), on_start_continuous=Mock(), on_stop_continuous=Mock(), **common)
        elif kind == "discovery":
            window = discovery_window.DiscoveryWindow(self.tk, discovery_fixtures.FakeManager(),
                on_start_continuous=Mock(), on_stop_continuous=Mock(), **common)
        else:
            window = auto_learning_window.AutoLearningWindow(self.tk, auto_fixtures.FakeManager(self.root),
                on_open_board=Mock(), **common)
        self.windows.append(window)
        return window

    @staticmethod
    def refresh(window):
        (window._refresh_now if hasattr(window, "_refresh_now") else window.refresh_now)()

    def test_three_views_share_live_counts_without_loading_reference_files(self):
        with patch.object(auto_learning_window.AutoLearningWindow, "_reference_summary",
                          side_effect=AssertionError("cached totals must not scan reference files")):
            views = [self.create(kind) for kind in ("online", "discovery", "auto")]
            for view in views:
                self.assertIn("视频 3 部", view.results_panel.summary.get())
                self.assertIn("部分完成 1", view.results_panel.summary.get())
                self.assertIn("累计跳过 2 个视频", view.results_panel.skipped.get())
            self.state["totals"] = totals(video_count=4, completed_count=3, visual_frame_count=24, skipped_count=3)
            before = self.snapshot.call_count
            for view in views:
                self.refresh(view)
                self.assertIn("视频 4 部", view.results_panel.summary.get())
                self.assertIn("24 帧", view.results_panel.summary.get())
                self.assertIn("累计跳过 3 个视频", view.results_panel.skipped.get())
            self.assertEqual(self.snapshot.call_count - before, 3)

    def test_small_windows_keep_results_controls_and_queue_visible(self):
        self.tk.deiconify()
        self.tk.geometry("980x700")
        for kind in ("online", "discovery", "auto"):
            with self.subTest(kind=kind):
                view = self.create(kind, embedded=kind == "online")
                if view.embedded:
                    view.win.pack(fill="both", expand=True)
                    surface = self.tk
                else:
                    view.win.geometry("980x700")
                    surface = view.win
                self.tk.update()
                right = surface.winfo_rootx() + surface.winfo_width()
                bottom = surface.winfo_rooty() + surface.winfo_height()
                widgets = [view.results_panel.summary_label, view.results_panel.skip_entry,
                           view.results_panel.details_button, view.tree]
                if kind != "auto":
                    widgets.extend((view.continuous_start_button, view.continuous_stop_button))
                for widget in widgets:
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom)
                self.assertGreater(view.tree.winfo_height(), 100)
                view.results_panel.details_button.invoke()
                self.tk.update()
                self.assertTrue(view.results_panel.details.winfo_ismapped())
                details = view.results_panel.details.get("1.0", "end")
                self.assertIn("跳过原因：会员或付费", details)
                self.assertIn("来源：https://search.video.iqiyi.com", details)
                self.assertNotIn("private-token", details)
                view.close()

    def test_skipped_download_filter_and_detail_do_not_report_completion(self):
        view = self.create("online")
        view.manager.jobs = [{"id": "skip", "url": "https://www.iqiyi.com/v_fixture.html", "title": "会员电影",
            "status": "skipped", "phase": "skipped", "progress": 100, "skip_reason": "需要会员权限",
            "message": "已跳过", "config": {"learning_kind": "story-reference", "learn": True}},
            {"id": "done", "url": "https://example.com/film", "status": "completed"}]
        self.refresh(view)
        view.filter.set("已跳过")
        self.assertEqual(view.tree.get_children(), ("skip",))
        self.assertEqual(view.tree.item("skip", "values")[0], "已跳过")
        self.assertEqual(view.tree.item("skip", "values")[2], "—")
        view.tree.selection_set("skip")
        view._selection_changed()
        self.assertIn("跳过原因\n需要会员权限", view.details_text.get("1.0", "end"))
        self.assertTrue(view.feed_button.instate(["disabled"]))

    def test_discovery_skipped_and_partial_rows_keep_reasons_above_stale_progress(self):
        view = self.create("discovery")
        view.manager.data["candidates"] = [
            {"id": "skip", "state": "skipped", "title": "受限电影", "url": "https://www.iqiyi.com/v_fixture.html",
             "category": "movie", "percent": 100, "skip_reason": "站点要求登录"},
            {"id": "partial", "state": "partial", "title": "片段", "category": "animation", "percent": 100,
             "detail": "已保存画面参考，字幕尚未就绪"}]
        self.refresh(view)
        view.filter.set("已跳过")
        self.assertEqual(view.tree.get_children(), ("skip",))
        self.assertEqual(view.tree.item("skip", "values")[-1], "站点要求登录")
        view.filter.set("部分完成")
        self.assertEqual(view.tree.get_children(), ("partial",))
        self.assertEqual(view.tree.item("partial", "values")[-2], "部分完成")
        self.assertIn("字幕尚未就绪", view.tree.item("partial", "values")[-1])

    def test_skipped_video_can_retry_explicitly_but_not_via_retry_failed(self):
        view = self.create("online")
        view.manager.jobs = [{"id": "skip", "url": "https://www.iqiyi.com/v_fixture.html", "status": "skipped"},
                             {"id": "done", "url": "https://example.com/film", "status": "completed"}]
        self.refresh(view)
        self.assertTrue(view.retry_failed_button.instate(["disabled"]))
        view.tree.selection_set("skip")
        menu = Mock()
        with patch.object(view.tree, "identify_row", return_value="skip"), patch("online_window.tk.Menu", return_value=menu):
            view._context_menu(Mock(y=0, x_root=0, y_root=0))
        retry_item = next(call.kwargs for call in menu.add_command.call_args_list if call.kwargs["label"].startswith("重试所选"))
        self.assertEqual(retry_item["state"], "normal")
        self.assertIn("跳过", retry_item["label"])
        retry_item["command"]()
        self.assertIn(("retry", ["skip"], {"request_version": 0}), view.manager.calls)
        view.manager.calls.clear()
        view.manager.jobs.append({"id": "fail", "url": "https://example.com/failure", "status": "failed"})
        self.refresh(view)
        self.assertTrue(view.retry_failed_button.instate(["!disabled"]))
        view.retry_failed_button.invoke()
        retry_calls = [call for call in view.manager.calls if call[0] == "retry"]
        self.assertEqual(retry_calls, [("retry", ["fail"], {"request_version": 0})])

    def test_unready_snapshot_cannot_leave_stale_success_counts(self):
        view = self.create("auto")
        self.state["totals"] = {"ready": False, "message": "统计暂未就绪"}
        self.refresh(view)
        self.assertIn("待统计", view.results_panel.summary.get())
        self.assertNotIn("视频 3 部", view.results_panel.summary.get())
        self.assertNotIn("2 个视频", view.results_panel.skipped.get())


if __name__ == "__main__":
    unittest.main()
