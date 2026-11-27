"""One-click generation uses a single worker and preserves editable local drafts."""
import copy
import gc
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

import story_pipeline
from story_window import StoryWindow
import theme


class StoryOneClickWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp = tempfile.TemporaryDirectory(prefix="story-oneclick-")
        self.root = Path(self.temp.name)/"project"
        self.root.mkdir()
        self.assets = Path(self.temp.name)/"AECPP"/"assets"
        self.assets.mkdir(parents=True)
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.temp.cleanup()
            self.skipTest(str(exc))
        theme.apply_theme(self.tk)
        self.tk.geometry("980x580+0+0")
        self.host = ttk.Frame(self.tk)
        self.host.pack(fill="both", expand=True)
        self.errors = []
        self.tk.report_callback_exception = lambda cls, exc, tb: self.errors.append(str(exc))
        self.messages = []
        self.message_patch = patch("story_window.messagebox.showerror", side_effect=lambda *args, **kwargs: self.messages.append(args))
        self.message_patch.start()
        self.can_write = Mock(return_value=True)
        self.window = StoryWindow(self.tk, self.root, container=self.host, can_write=self.can_write)
        self.window.win.pack(fill="both", expand=True)
        self.text = "场景：客厅\n小明：今天终于不用上班了！"
        self.window.source.insert("1.0", self.text)
        self.window.source_format.set("角色：台词")
        self.board = story_pipeline.generate_storyboard(self.text, "我的沙雕动画", source_format="script")
        cid = self.board["characters"][0]["id"]
        self.catalog = {"version": 1, "root": str(self.assets), "actors": [{
            "id": "hero.actor", "name": "小明", "path": str(self.assets/"hero.actor"),
            "relative_path": "hero.actor", "sha256": "fixture", "dependencies": [], "valid": True, "issues": [],
            "actions": [{"id": "idle-real", "name": "站立", "loop": True, "duration_frames": 30},
                        {"id": "talk-real", "name": "说话", "loop": True, "duration_frames": 30}]}],
            "backgrounds": [{"id": "room.png", "name": "客厅", "path": str(self.assets/"room.png"),
                             "relative_path": "room.png", "sha256": "fixture"}],
            "expressions": [{"id": "neutral.png", "name": "平静", "path": str(self.assets/"neutral.png"),
                             "builtin": False, "sha256": "fixture"}], "issues": []}
        self.bindings = {"version": 1, "resource_root": str(self.assets), "characters": {cid: "hero.actor"},
                         "backgrounds": {"客厅": "room.png"}, "actions": {cid: {"talk": "talk-real", "idle": "idle-real"}},
                         "emotions": {cid: {"neutral": "neutral.png", "happy": "neutral.png"}}}
        self.tk.update()

    def tearDown(self):
        self.wait_idle()
        self.can_write.return_value = True
        if not self.window._closed:
            self.window._selected = None
            self.window.close()
        self.tk.report_callback_exception = None
        self.tk.destroy()
        self.message_patch.stop()
        self.host = self.window = self.tk = None
        gc.collect()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic()+8
        while self.window.busy and time.monotonic()<deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.errors)

    def result(self):
        run_dir = self.root/"creations"/"animations"/"fixture"
        run_dir.mkdir(parents=True, exist_ok=True)
        output = run_dir/"story.aecpp"
        output.write_text("fixture native project", encoding="utf-8")
        report_path = run_dir/"matching_report.json"
        report_path.write_text("{}", encoding="utf-8")
        return {"board": copy.deepcopy(self.board), "catalog": copy.deepcopy(self.catalog),
                "bindings": copy.deepcopy(self.bindings), "plan": {"ready": True}, "output": output,
                "run_dir": run_dir, "report_path": report_path, "warnings": ["开心表情使用现有平静表情替代。"], "matches": []}

    def real_library(self):
        """Native-format fixture resources, processed by the unmocked pipeline."""
        from test_aecpp_resources import ResourceTests
        fixture = ResourceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root/"project"
        self.root.mkdir()
        self.window.root = self.root
        self.assets = fixture.assets
        return fixture

    def replace_source(self, text):
        self.window.source.delete("1.0", "end")
        self.window.source.insert("1.0", text)

    def reopen(self):
        self.assertTrue(self.window.close())
        self.window = StoryWindow(self.tk, self.root, container=self.host, can_write=self.can_write)
        self.window.win.pack(fill="both", expand=True)

    def test_single_worker_uses_plain_snapshots_without_confirmation_or_save_dialog(self):
        main = threading.get_ident()
        self.window.resource_catalog = copy.deepcopy(self.catalog)
        self.window.resource_config = copy.deepcopy(self.bindings)
        self.window.resource_root.set(str(self.assets))
        calls = []

        def generate(root, text, title, **kwargs):
            self.assertNotEqual(threading.get_ident(), main)
            self.assertEqual((root, text, title), (self.root, self.text, "我的沙雕动画"))
            self.assertEqual(kwargs["source_format"], "script")
            self.assertEqual(kwargs["resource_root"], str(self.assets))
            self.assertIsNot(kwargs["bindings"], self.window.resource_config)
            self.assertIsNot(kwargs["catalog"], self.window.resource_catalog)
            self.assertIsInstance(kwargs["stop"], threading.Event)
            kwargs["report"]("正在组装真实素材")
            calls.append(kwargs)
            return self.result()

        var_get, text_get = tk.StringVar.get, tk.Text.get
        def guarded_var(variable, *args):
            self.assertEqual(threading.get_ident(), main, "worker must never read a Tk variable")
            return var_get(variable, *args)
        def guarded_text(widget, *args):
            self.assertEqual(threading.get_ident(), main, "worker must never read a Tk text")
            return text_get(widget, *args)
        with patch("story_automation.generate_animation", side_effect=generate), \
             patch("story_window.messagebox.askyesno") as confirm, \
             patch("story_window.filedialog.asksaveasfilename") as save_dialog, \
             patch("story_window.filedialog.askdirectory") as directory_dialog, \
             patch.object(tk.StringVar, "get", guarded_var), patch.object(tk.Text, "get", guarded_text):
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        self.assertEqual(len(calls), 1)
        confirm.assert_not_called()
        save_dialog.assert_not_called()
        directory_dialog.assert_not_called()
        self.assertEqual(self.window.board, story_pipeline.load_storyboard(self.window.board_path))
        self.assertEqual(self.window.resource_config["characters"], self.bindings["characters"])
        self.assertEqual(self.window.tabs.index("current"), 2)
        self.assertIn("story.aecpp", self.window.status.get())
        self.assertIn("替代", self.window.resource_detail.get("1.0", "end-1c"))
        self.assertEqual(str(self.window.open_output_button.cget("state")), "normal")
        self.assertFalse(self.messages)

    def test_existing_unsaved_edits_are_backed_up_before_success_replaces_board(self):
        self.window.board = story_pipeline.generate_storyboard("小王：这是旧故事。", source_format="script")
        self.window._render()
        self.window.dialogue.delete("1.0", "end")
        self.window.dialogue.insert("1.0", "这条未保存的修正需要备份。")
        with patch("story_automation.generate_animation", side_effect=lambda *args, **kwargs: self.result()), \
             patch("story_window.messagebox.askyesno") as confirm:
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        confirm.assert_not_called()
        backup = self.window._last_generate_backup
        previous = story_pipeline.load_storyboard(backup/"current_story.json")
        self.assertEqual(previous["shots"][0]["text"], "这条未保存的修正需要备份。")
        self.assertTrue((backup/"story_draft.json").is_file())
        self.assertEqual(self.window.board["shots"][0]["text"], self.board["shots"][0]["text"])
        self.assertFalse(self.messages)

    def test_cancel_and_close_preserve_previous_board_and_allow_retry(self):
        self.window.board = copy.deepcopy(self.board)
        self.window._render()
        old = copy.deepcopy(self.window.board)
        entered = threading.Event()
        def generate(*args, stop, **kwargs):
            entered.set()
            if not stop.wait(4):
                raise RuntimeError("取消事件没有送达")
            raise ValueError("已取消本次动画生成")
        with patch("story_automation.generate_animation", side_effect=generate):
            self.assertTrue(self.window._generate_animation())
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.window.close())
            self.wait_idle()
        self.assertTrue(self.window.win.winfo_exists())
        self.assertEqual(self.window.board, old)
        self.assertEqual(story_pipeline.load_storyboard(self.window.board_path), old)
        self.assertIn("已取消", self.window.status.get())
        self.assertFalse(self.messages)
        with patch("story_automation.generate_animation", side_effect=lambda *args, **kwargs: self.result()):
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        self.assertIn("已生成", self.window.status.get())

    def test_missing_directory_guides_setup_then_reuses_saved_directory_on_retry(self):
        with patch("story_automation.generate_animation", side_effect=ValueError("未找到 AECPP 素材目录")):
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        self.assertEqual(self.window.tabs.index("current"), 2)
        self.assertIn("选择 AECPP 素材目录并保存设置", self.window.status.get())
        self.assertEqual(str(self.window.oneclick_button.cget("state")), "normal")
        self.window.resource_root.set(str(self.assets))
        self.assertTrue(self.window._save_resources())
        self.assertTrue(self.window.close())
        self.window = StoryWindow(self.tk, self.root, container=self.host, can_write=self.can_write)
        self.window.win.pack(fill="both", expand=True)
        self.assertEqual(self.window.resource_root.get(), str(self.assets))
        with patch("story_automation.generate_animation", side_effect=lambda *args, **kwargs: self.result()) as generate:
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        self.assertIsNone(generate.call_args.kwargs["resource_root"])
        self.assertEqual(generate.call_args.kwargs["bindings"]["resource_root"], str(self.assets))

    def test_export_failure_keeps_old_draft_and_finished_publication_wins_late_cancel(self):
        self.window.board = copy.deepcopy(self.board)
        self.window._render()
        self.window.dialogue.delete("1.0", "end")
        self.window.dialogue.insert("1.0", "导出失败也不能丢失的修正。")
        with patch("story_automation.generate_animation", side_effect=OSError("素材复制失败")):
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        self.assertEqual(self.window.board["shots"][0]["text"], "导出失败也不能丢失的修正。")
        self.assertEqual(story_pipeline.load_storyboard(self.window.board_path), self.window.board)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(str(self.window.oneclick_button.cget("state")), "normal")
        def published(*args, stop, **kwargs):
            result = self.result()
            stop.set()  # Publication succeeded before a final cancellation arrived.
            return result
        with patch("story_automation.generate_animation", side_effect=published):
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        self.assertIn("已生成", self.window.status.get())
        self.assertEqual(self.window.board["shots"][0]["text"], self.board["shots"][0]["text"])

    def test_busy_storage_guard_and_empty_text_do_not_start_another_worker(self):
        with patch("story_automation.generate_animation") as generate:
            self.can_write.return_value = False
            self.assertFalse(self.window._generate_animation())
            self.can_write.return_value = True
            self.window.source.delete("1.0", "end")
            self.assertFalse(self.window._generate_animation())
        generate.assert_not_called()
        self.window.source.insert("1.0", self.text)
        release = threading.Event()
        def work(*args, **kwargs):
            release.wait(4)
            return self.result()
        with patch("story_automation.generate_animation", side_effect=work) as generate:
            try:
                self.assertTrue(self.window._generate_animation())
                self.assertFalse(self.window._generate_animation())
                self.assertEqual(str(self.window.oneclick_button.cget("state")), "disabled")
                self.assertEqual(str(self.window.cancel_generation_button.cget("state")), "normal")
            finally:
                release.set()
            self.wait_idle()
        self.assertEqual(generate.call_count, 1)

    def test_open_output_and_small_layout_are_usable_and_last_output_survives_reopen(self):
        for button in (self.window.oneclick_button, self.window.generate_button, self.window.cancel_generation_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_rootx(), self.window.win.winfo_rootx())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(), self.window.win.winfo_rootx()+self.window.win.winfo_width())
        self.assertGreater(self.window.source.winfo_height(), 90)
        with patch("story_automation.generate_animation", side_effect=lambda *args, **kwargs: self.result()):
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        directory = self.window._last_animation_output.parent
        with patch("story_window.os.startfile", create=True) as open_folder:
            self.assertTrue(self.window._open_animation_directory())
        open_folder.assert_called_once_with(str(directory))
        self.assertTrue(self.window.close())
        self.window = StoryWindow(self.tk, self.root, container=self.host, can_write=self.can_write)
        self.window.win.pack(fill="both", expand=True)
        self.assertEqual(self.window._last_animation_output.parent, directory)
        self.assertEqual(str(self.window.open_output_button.cget("state")), "normal")

    def test_real_pipeline_reopens_saved_directory_and_remaps_named_roles_across_stories(self):
        fixture = self.real_library()
        teacher = copy.deepcopy(fixture.document)
        teacher["name"] = "老师"
        teacher_file = fixture.assets/"actors"/"老师"/"老师.actor"
        fixture.write(teacher_file, teacher)
        self.replace_source("场景：教室\n老师：准备上课了。")
        self.assertTrue(self.window._generate_animation())
        self.wait_idle()
        self.assertFalse(self.messages)
        first = self.window._last_animation_output
        self.assertTrue(first.is_file())
        old_project = first.read_bytes()
        self.assertTrue(self.window._save_resources())
        from aecpp_resources import load_bindings
        saved = load_bindings(self.root)
        self.assertEqual(saved["character_names"], {"character-1": "老师"})
        teacher_id = saved["characters"]["character-1"]
        self.reopen()
        self.assertEqual(self.window.resource_root.get(), str(fixture.assets.resolve()))
        self.replace_source("场景：教室\n小明：我来了。\n老师：请坐。")
        self.assertTrue(self.window._generate_animation())
        self.wait_idle()
        self.assertFalse(self.messages)
        output = self.window._last_animation_output
        self.assertNotEqual(output, first)
        self.assertEqual(first.read_bytes(), old_project)
        role_ids = {row["name"]: row["id"] for row in self.window.board["characters"]}
        self.assertEqual(self.window.resource_config["characters"][role_ids["老师"]], teacher_id)
        self.assertNotEqual(self.window.resource_config["characters"][role_ids["小明"]], teacher_id)
        report = json.loads((output.parent/"matching-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["output"], str(output))
        self.assertEqual(report["resource_root"], self.window.resource_catalog["root"])
        self.assertEqual(report["warnings"], self.window._automation_warnings)
        bindings = json.loads((output.parent/"bindings.json").read_text(encoding="utf-8"))
        self.assertEqual(bindings, self.window.resource_config)
        self.assertEqual(story_pipeline.load_storyboard(output.parent/"storyboard.json"), self.window.board)
        project = json.loads(output.read_text(encoding="utf-8"))
        actors = [row for row in project["scene"]["layers"] if row["contentKind"]==3]
        self.assertEqual(len(actors), 2)
        for actor in actors:
            self.assertTrue((output.parent/actor["actor"]["resourcePath"]).is_file())

    def test_real_pipeline_recovers_a_stale_restored_directory_without_overriding_new_input(self):
        fixture = self.real_library()
        from aecpp_resources import save_bindings
        missing = str(self.root/"old-machine"/"AECPP")
        save_bindings(self.root, {"resource_root": missing})
        self.reopen()
        self.assertEqual(self.window.resource_root.get(), missing)
        self.assertTrue(self.window._generate_animation())
        self.wait_idle()
        self.assertFalse(self.messages)
        self.assertEqual(self.window.resource_catalog["root"], str(fixture.assets.resolve()))
        self.assertTrue(any("不可用" in warning for warning in self.window._automation_warnings))
        previous = self.window._last_animation_output
        self.window.resource_root.set(str(self.root/"new-invalid-choice"))
        self.assertTrue(self.window._generate_animation())
        self.wait_idle()
        self.assertEqual(self.window._last_animation_output, previous)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("素材目录不存在", self.window.status.get())

    def test_real_copy_cancellation_preserves_draft_and_cleans_unpublished_run(self):
        self.real_library()
        self.window.board = copy.deepcopy(self.board)
        self.window._render()
        previous = copy.deepcopy(self.window.board)
        put = self.window._events.put
        def cancel_on_copy(item, *args, **kwargs):
            if item[0]=="progress" and "复制现有角色" in item[1]:
                self.window._generation_stop.set()
            return put(item, *args, **kwargs)
        with patch.object(self.window._events, "put", side_effect=cancel_on_copy):
            self.assertTrue(self.window._generate_animation())
            self.wait_idle()
        self.assertFalse(self.messages)
        self.assertIn("已取消", self.window.status.get())
        self.assertEqual(self.window.board, previous)
        self.assertEqual(story_pipeline.load_storyboard(self.window.board_path), previous)
        self.assertFalse(list((self.root/"creations"/"animations").iterdir()))
        self.assertTrue(self.window._generate_animation())
        self.wait_idle()
        self.assertTrue(self.window._last_animation_output.is_file())


if __name__ == "__main__":
    unittest.main()
