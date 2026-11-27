"""Text creation workspace regression tests use isolated temporary projects."""
import gc
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import story_pipeline
from story_window import StoryWindow, ACTIONS, EMOTIONS
import theme


class StoryWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()  # Collect destroyed Tk fixtures on the UI thread, never a worker.
        self.temp = tempfile.TemporaryDirectory(prefix="yingge-story-window-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        theme.apply_theme(self.tk)
        self.tk.geometry("980x650+0+0")
        self.host = ttk.Frame(self.tk)
        self.host.pack(fill="both", expand=True)
        self.errors = []
        self.tk.report_callback_exception = lambda cls, exc, tb: self.errors.append(str(exc))
        self.messages = []
        self.error_patch = patch("story_window.messagebox.showerror", side_effect=lambda *args, **kwargs: self.messages.append(args))
        self.error_patch.start()
        self.addCleanup(self.error_patch.stop)
        self.can_write = Mock(return_value=True)
        self.closed = Mock()
        self.window = StoryWindow(self.tk, self.root, container=self.host, can_write=self.can_write, on_close=self.closed)
        self.window.win.pack(fill="both", expand=True)
        self.addCleanup(self.cleanup)
        self.tk.update()

    def cleanup(self):
        self.wait_idle()
        self.can_write.return_value = True
        if not self.window._closed:
            self.window._selected = None
            self.window.close()
        self.tk.report_callback_exception = None
        self.tk.destroy()
        self.host = self.window = self.tk = None
        gc.collect()

    def wait_idle(self):
        deadline = time.monotonic()+8
        while self.window.busy and time.monotonic()<deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.errors)

    def generate(self):
        self.window.source.insert("1.0", "场景：客厅\n小明：我要睡觉！\n小王：现在才中午。")
        self.window._generate()
        self.wait_idle()
        self.assertIsNotNone(self.window.board)
        self.assertFalse(self.messages)

    def test_generation_edits_new_role_and_drafts_survive_reopen(self):
        self.generate()
        first = self.window.board["shots"][0]["id"]
        self.window.speaker.set("新角色")
        self.window.action.set(ACTIONS["jump"])
        self.window.emotion.set(EMOTIONS["shocked"])
        self.window.dialogue.delete("1.0", "end")
        self.window.dialogue.insert("1.0", "怎么又要上班了！")
        self.window.duration.set("4.5")
        self.window._apply()
        stored = story_pipeline.load_storyboard(self.window.board_path)
        self.assertEqual(stored["shots"][0]["action"], "jump")
        self.assertEqual(stored["shots"][0]["duration"], 4.5)
        self.assertEqual(stored["shots"][0]["text"], "怎么又要上班了！")
        self.assertTrue(any(c["name"]=="新角色" for c in stored["characters"]))
        self.assertEqual(stored["shots"][0]["actor"], stored["shots"][0]["speaker"])
        source = self.window.source.get("1.0", "end-1c")
        self.assertTrue(self.window.close())
        self.assertTrue(self.tk.winfo_exists())
        self.window = StoryWindow(self.tk, self.root, container=self.host)
        self.window.win.pack(fill="both", expand=True)
        self.assertEqual(self.window.source.get("1.0", "end-1c"), source)
        self.assertEqual(self.window.board["shots"][0]["id"], first)
        self.assertEqual(self.window.board["shots"][0]["action"], "jump")

    def test_selecting_another_shot_commits_editor_without_losing_text(self):
        self.generate()
        first, second = self.window.board["shots"][:2]
        self.window.dialogue.delete("1.0", "end")
        self.window.dialogue.insert("1.0", "这次修改必须保留。")
        self.window.tree.selection_set(second["id"])
        self.window._selection()
        self.assertEqual(self.window.board["shots"][0]["text"], "这次修改必须保留。")
        self.assertEqual(self.window._selected, second["id"])
        self.window.tree.selection_set(first["id"])
        self.window._selection()
        self.assertEqual(self.window.dialogue.get("1.0", "end-1c"), "这次修改必须保留。")

    def test_repeated_save_after_speaker_change_keeps_synced_actor(self):
        self.generate()
        self.window.speaker.set("小李")
        self.assertTrue(self.window.save_current())
        self.assertEqual(self.window.actor.get(), "小李")
        self.assertTrue(self.window.save_current())
        shot = story_pipeline.load_storyboard(self.window.board_path)["shots"][0]
        self.assertEqual(shot["actor"], shot["speaker"])
        self.assertEqual(self.window.tree.item(shot["id"], "values")[0], "小李")

    def test_invalid_edit_does_not_overwrite_saved_board(self):
        self.generate()
        original = self.window.board_path.read_bytes()
        self.window.duration.set("nan")
        self.assertFalse(self.window.save_current())
        self.assertEqual(self.window.board_path.read_bytes(), original)
        self.assertTrue(self.messages)
        self.window.duration.set("3")

    def test_add_reorder_delete_and_keep_one_shot(self):
        self.generate()
        original = [s["id"] for s in self.window.board["shots"]]
        self.window._add()
        added = self.window._selected
        self.assertEqual(len(self.window.board["shots"]), len(original)+1)
        self.window._move(-1)
        self.assertEqual(self.window.board["shots"][0]["id"], added)
        self.window._delete()
        self.assertEqual([s["id"] for s in self.window.board["shots"]], original)
        while len(self.window.board["shots"])>1:
            self.window._delete()
        self.window._delete()
        self.assertEqual(len(self.window.board["shots"]), 1)

    def test_busy_close_is_blocked_and_worker_errors_restore_controls(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def fail():
            release.wait(4)
            raise ValueError("模拟导出失败")
        self.window._run("export", fail)
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.win.winfo_exists())
        self.assertEqual(str(self.window.export_button.cget("state")), "disabled")
        release.set()
        self.wait_idle()
        self.assertEqual(str(self.window.export_button.cget("state")), "normal")
        self.assertIn("模拟导出失败", self.window.status.get())
        self.assertEqual(len(self.messages), 1)

    def test_storage_guard_prevents_generation_and_persistence(self):
        self.can_write.return_value = False
        self.window.source.insert("1.0", "小明：你好！")
        self.window._generate()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.board_path.exists())
        self.assertFalse(self.window.draft_path.exists())
        self.assertFalse(self.window.save_current())

    def test_export_is_real_aecpp_and_learning_requires_confirmation(self):
        self.generate()
        output = self.root / "我的动画.aecpp"
        with patch("story_window.filedialog.asksaveasfilename", return_value=str(output)):
            self.window._export()
        self.wait_idle()
        self.assertTrue(output.is_file())
        self.assertFalse(self.messages)
        script = self.root / "角色剧本.json"
        with patch("story_window.filedialog.asksaveasfilename", return_value=str(script)):
            self.window._export_script()
        self.wait_idle()
        self.assertTrue(script.is_file())
        with patch("story_window.messagebox.askyesno", return_value=False), patch.object(story_pipeline, "train_style") as train:
            self.window._train()
        train.assert_not_called()
        self.window.action.set(ACTIONS["shake"])
        with patch("story_window.messagebox.askyesno", return_value=True):
            self.window._train()
        self.wait_idle()
        self.assertIsNotNone(story_pipeline.load_style(self.root))
        self.assertFalse(self.messages)

    def test_narrative_action_actor_can_be_changed_independently(self):
        self.window.source.insert("1.0", "小明走进办公室。")
        self.window.source_format.set("小说 / 普通文本")
        self.window._generate()
        self.wait_idle()
        self.window.actor.set("小王")
        self.window._apply()
        saved = story_pipeline.load_storyboard(self.window.board_path)
        self.assertEqual(saved["shots"][0]["speaker"], "")
        actor = saved["shots"][0]["actor"]
        self.assertEqual(next(c["name"] for c in saved["characters"] if c["id"]==actor), "小王")

    def test_destroy_cancels_timer_and_minimum_layout_shows_actions(self):
        self.generate()
        self.tk.geometry("980x580+0+0")
        self.tk.update()
        self.assertGreater(self.window.tree.winfo_height(), 80)
        self.assertGreater(self.window.dialogue.winfo_height(), 40)
        self.window.status.set("很长的导出说明与文件路径/"*100)
        self.tk.update()
        self.assertGreater(self.window.dialogue.winfo_height(), 40)
        top = self.window.win.winfo_rooty()
        bottom = top+self.window.win.winfo_height()
        for button in (self.window.learn_button, self.window.export_button, self.window.script_export_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_rooty(), top)
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(), bottom)
        timer = self.window._timer
        self.window.win.destroy()
        self.tk.update()
        self.assertTrue(self.window._closed)
        self.assertNotIn(timer, self.tk.tk.call("after", "info"))
        self.closed.assert_called_once()
        self.assertFalse(self.errors)

    def test_open_automatic_board_preserves_current_edits_in_backup(self):
        self.generate()
        self.window.dialogue.delete("1.0", "end")
        self.window.dialogue.insert("1.0", "尚未保存的修改也要备份。")
        incoming = story_pipeline.generate_storyboard("小李：今天放假了。", "视频参考")
        incoming["provenance"] = {"kind": "automatic-video-reference", "review_status": "unreviewed", "movie_id": 5}
        incoming["shots"][0].update(source_start_ms=1000, source_end_ms=4000, source_kind="subtitle")
        path = self.root / "自动参考.json"
        story_pipeline.save_storyboard(incoming, path)
        self.assertTrue(self.window.open_board(path))
        backup = self.window._last_open_backup
        previous = story_pipeline.load_storyboard(backup / "current_story.json")
        self.assertEqual(previous["shots"][0]["text"], "尚未保存的修改也要备份。")
        self.assertTrue((backup / "story_draft.json").is_file())
        self.assertEqual(self.window.title.get(), "视频参考")
        self.assertEqual(story_pipeline.load_storyboard(self.window.board_path), incoming)
        self.assertIn("规则建议", self.window.status.get())
        self.assertIsNone(story_pipeline.load_style(self.root))
        self.assertEqual(self.tk.call(self.window.tabs, "index", "current"), 1)

    def test_invalid_auto_board_does_not_replace_or_backup_work(self):
        self.generate()
        before = self.window.board_path.read_bytes()
        path = self.root / "损坏.json"
        path.write_text("{}", encoding="utf-8")
        self.assertFalse(self.window.open_board(path))
        self.assertEqual(self.window.board_path.read_bytes(), before)
        self.assertFalse((self.root / "creations/draft_history").exists())

    def test_generation_consumes_reference_and_displays_its_limited_scope(self):
        module = SimpleNamespace(
            load_reference=lambda root: {"example_count": 12, "movie_count": 1, "validation": {"kind": "none"}},
            predict_reference_duration=lambda model, text: 7.)
        with patch.dict("sys.modules", {"auto_learning": module}):
            self.generate()
        self.assertEqual(self.window.board["shots"][0]["duration"], 7.)
        self.assertIn("12 条样例", self.window.status.get())
        self.assertIn("不代表动作理解", self.window.status.get())
        self.assertIn("尚无独立影片验证", self.window.status.get())


if __name__=="__main__":
    unittest.main()
