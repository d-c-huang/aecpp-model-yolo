"""Persistent-mode UI snapshots and controls; no network or background workers."""
import copy
import gc
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import dataset
import discovery_window
import online_window
import test_discovery_window as discovery_fixtures
import test_online_window as online_fixtures
import theme


class ContinuousWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory(prefix="continuous-window-")
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
        self.addCleanup(self.cleanup)
        self.state = {"enabled": False, "busy": False, "state": "idle", "round_count": 0}
        self.writable = True
        self.starts, self.stops = Mock(side_effect=self.start), Mock(side_effect=self.stop)
        self.manager = online_fixtures.FakeManager(self.root)
        self.discovery = discovery_fixtures.FakeManager()

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

    def start(self):
        self.state.update(enabled=True, busy=True, state="preparing", message="检查本机 Ollama")
        return True

    def stop(self):
        self.state.update(enabled=False, busy=False, state="paused", message="下载与分析已暂停，学习成果保留")
        return True

    def online(self, **kwargs):
        window = online_window.OnlineWindow(self.tk, self.manager,
            on_open_movie=Mock(), on_learn=Mock(), on_open_training=Mock(), on_auto_learn=Mock(),
            on_auto_learning=Mock(), on_discovery=Mock(), can_download=lambda:self.writable,
            on_start_continuous=self.starts, on_stop_continuous=self.stops,
            continuous_status=lambda:copy.deepcopy(self.state), **kwargs)
        self.windows.append(window)
        return window

    def discovered(self, **kwargs):
        window = discovery_window.DiscoveryWindow(self.tk,self.discovery,
            on_start_continuous=self.starts,on_stop_continuous=self.stops,
            continuous_status=lambda:copy.deepcopy(self.state),can_write=lambda:self.writable,**kwargs)
        self.windows.append(window)
        return window

    def test_single_start_needs_no_settings_and_stop_remains_available_when_storage_busy(self):
        window = self.online()
        self.starts.assert_not_called()
        window.continuous_start_button.invoke()
        self.starts.assert_called_once_with()
        self.assertTrue(window.continuous_start_button.instate(["disabled"]))
        self.assertTrue(window.continuous_stop_button.instate(["!disabled"]))
        window._start_continuous()
        self.starts.assert_called_once_with()
        self.writable = False
        window._refresh_now()
        window.continuous_stop_button.invoke()
        self.stops.assert_called_once_with()
        self.assertIn("保留",window.status.get())
        self.assertTrue(window.continuous_start_button.instate(["disabled"]))
        self.writable = True
        window._refresh_now()
        self.assertEqual(window.continuous_start_button.cget("text"),"继续自动学习")
        window.continuous_start_button.invoke()
        self.assertEqual(self.starts.call_count,2)

    def test_restart_snapshot_does_not_start_network_and_can_continue(self):
        self.state.update(state="paused",round_count=7,message="上次运行已暂停")
        window = self.online()
        self.starts.assert_not_called()
        self.assertIn("第 7 轮",window.continuous_summary.get())
        self.assertEqual(window.continuous_start_button.cget("text"),"继续自动学习")
        window.continuous_start_button.invoke()
        self.starts.assert_called_once_with()

    def test_live_sources_model_bytes_download_and_learning_are_not_fake_percentages(self):
        self.state.update(enabled=True,busy=True,state="preparing",message="下载视觉模型",round_count=2,
            current_url="http://127.0.0.1:11434/api/pull?api_key=private-key",
            progress={"completed":1048576,"total":4194304},
            current_downloads=[{"title":"动画一","message":"正在下载 2 MB"}],
            current_learning=[{"title":"电影一","message":"画面 00:08–00:16，识别动作"}])
        window = self.online()
        self.assertIn("1.0 MB / 4.0 MB",window.continuous_summary.get())
        self.assertIn("动画一",window.continuous_summary.get())
        self.assertIn("00:08–00:16",window.continuous_summary.get())
        self.assertNotIn("private-key",window.continuous_source.get())
        self.state.update(state="waiting",busy=False,progress={},next_run_seconds=45,message="本轮已完成")
        window._refresh_now()
        self.assertIn("45 秒后",window.continuous_summary.get())
        self.assertNotIn("100%",window.continuous_summary.get())
        self.assertTrue(window.continuous_start_button.instate(["disabled"]))
        self.assertTrue(window.continuous_stop_button.instate(["!disabled"]))

    def test_embedded_980x700_keeps_primary_buttons_sources_and_queue_visible(self):
        self.tk.deiconify()
        self.tk.geometry("980x700")
        self.state.update(enabled=True,state="backpressure",round_count=12,
            message="等待现有下载和分析任务完成后继续查找",next_run_in=60,
            current_downloads=[{"title":"沙雕动画","message":"读取原视频，当前 120 MB"}],
            current_learning=[{"title":"喜剧电影","message":"分析 00:08–00:16 的动作和表情"}])
        window = self.online(container=self.tk)
        window.win.pack(fill="both",expand=True)
        self.tk.update()
        right,bottom=self.tk.winfo_rootx()+980,self.tk.winfo_rooty()+700
        for widget in (window.continuous_start_button,window.continuous_stop_button,
                       window.continuous_source_entry,window.tree,window.feed_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),right)
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bottom)
        self.assertGreater(window.tree.winfo_height(),100)

    def test_discovery_running_blocks_manual_round_and_queue_then_restores_advanced_settings(self):
        queued = Mock()
        self.state.update(enabled=True,busy=False,state="waiting",next_run_in=45)
        window = self.discovered(on_queue_selected=queued)
        self.discovery.data["candidates"] = [discovery_fixtures.DiscoveryWindowTests.candidate()]
        window.refresh_now()
        window.tree.selection_set("video")
        window._selection_changed()
        self.assertTrue(window.queue_button.instate(["disabled"]))
        window._start()
        window._queue_selected()
        self.assertEqual(self.discovery.calls,[])
        queued.assert_not_called()
        self.assertTrue(window.start_button.instate(["disabled"]))
        self.assertTrue(window.single_round_toggle.instate(["disabled"]))
        window.continuous_stop_button.invoke()
        self.stops.assert_called_once_with()
        window.single_round_toggle.invoke()
        self.tk.update_idletasks()
        self.assertTrue(window._single_round_expanded)
        window.start_button.invoke()
        self.assertEqual(self.discovery.calls[0][0],"start")

    def test_discovery_model_preparation_and_active_analysis_share_visible_detail(self):
        self.state.update(enabled=True,busy=True,state="preparing",message="下载语音模型 tiny",
            current_url="https://example.com/model?token=secret",progress={"completed":10,"total":20})
        window = self.discovered(activity=lambda:{"current_downloads":[{"title":"动画","percent":12}],
            "current_learning":[{"title":"电影","message":"实际画面片段 00:16–00:24"}]})
        self.assertIn("tiny",window.current_source.get())
        self.assertNotIn("secret",window.current_url.get())
        self.assertIn("12.0%",window.download_activity.get())
        self.assertIn("00:16–00:24",window.learning_activity.get())
        window.win.geometry("980x700")
        self.tk.update()
        bottom=window.win.winfo_rooty()+window.win.winfo_height()
        for widget in (window.continuous_start_button,window.continuous_stop_button,
                       window.current_url_entry,window.tree,window.learning_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bottom)
        self.assertGreater(window.tree.winfo_height(),100)

    def test_start_failure_is_visible_and_retryable_without_destroying_queue(self):
        window = self.online()
        self.starts.side_effect=ValueError("模型目录不可写")
        with patch("online_window.messagebox.showerror") as error:
            window.continuous_start_button.invoke()
        error.assert_called_once()
        self.assertIn("模型目录不可写",window.status.get())
        self.assertTrue(window.continuous_start_button.instate(["!disabled"]))
        self.assertEqual(self.manager.jobs,[])

    def test_closing_views_does_not_stop_continuous_manager(self):
        self.state.update(enabled=True,busy=True,state="searching")
        online,discovery=self.online(),self.discovered()
        online.close();discovery.close()
        self.stops.assert_not_called()
        self.assertTrue(self.state["enabled"])
        self.assertIsNone(online._timer)
        self.assertIsNone(discovery._timer)


if __name__ == "__main__":
    unittest.main()
