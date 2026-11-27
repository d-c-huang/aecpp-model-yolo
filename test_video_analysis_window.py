"""Real Tk checks for local evidence navigation and explicit model operations."""
import gc
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import dataset
import theme
from test_video_analysis_notes import result_fixture
from video_analysis_notes import save_reference
from video_analysis_window import VideoAnalysisWindow


class VideoAnalysisWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/"project"
        dataset.initialize(self.root)
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode) VALUES(1,'one','分析影片','time')")
            db.execute("INSERT INTO movies(id,digest,name,mode) VALUES(2,'two','另一影片','time')")
        self.image = self.root/"video_analysis/keyframes/frame.jpg"
        self.image.parent.mkdir(parents=True)
        Image.new("RGB",(320,180),(60,80,100)).save(self.image)
        result = result_fixture()
        result["contentBlocks"][0]["highlights"][0]["keyframes"][0]["path"] = self.image.relative_to(self.root).as_posix()
        result["contentBlocks"][0]["evidenceSegments"] = [{"startMs":1000,"endMs":2000,"text":"真实转写内容"}]
        self.note = save_reference(self.root,1,result,source={"kind":"builtin","model":"fixture"})
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.temp.cleanup()
            self.skipTest(str(exc))
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.errors = []
        self.tk.report_callback_exception = lambda cls,exc,tb:self.errors.append(str(exc))
        self.closed, self.saved, self.detected, self.downloaded, self.opened = [Mock() for _ in range(5)]
        self.window = VideoAnalysisWindow(self.tk,self.root,on_close=self.closed,on_save=self.saved,
            on_test_models=self.detected,on_download_asr=self.downloaded,on_open_board=self.opened,movie_id=1)
        self.tk.update()

    def tearDown(self):
        self.window.close()
        self.tk.report_callback_exception = None
        self.tk.destroy()
        self.window = self.tk = None
        gc.collect()
        self.temp.cleanup()

    def test_open_only_reads_local_results_and_preserves_time_and_evidence(self):
        self.saved.assert_not_called()
        self.detected.assert_not_called()
        self.downloaded.assert_not_called()
        self.opened.assert_not_called()
        self.assertEqual(self.window.block_tree.get_children(),("b1","b2"))
        self.assertEqual(self.window.block_tree.item("b1","values")[0],"00:00:00")
        detail = self.window.detail.get("1.0","end")
        self.assertIn("真实转写内容",detail)
        self.assertIn("不是原始台词",detail)
        self.assertIsNotNone(self.window._photo)
        self.assertEqual(self.window.detail.cget("state"),"disabled")

    def test_mindmap_click_navigates_to_block_and_multiselect_is_explicit(self):
        self.window.map_tree.selection_set("n2")
        self.window._select_map_node()
        self.assertEqual(self.window.block_tree.selection(),("b1",))
        self.window.block_tree.selection_set(("b1","b2"))
        self.window._select_block()
        self.assertIn("已选择 2 个内容段",self.window.detail.get("1.0","end"))
        self.opened.assert_not_called()
        self.window.open_selected_button.invoke()
        self.opened.assert_called_once_with(self.note["path"],["b1","b2"])
        self.window.open_all_button.invoke()
        self.assertEqual(self.opened.call_args.args,(self.note["path"],None))

    def test_settings_callbacks_do_not_persist_or_leak_the_runtime_key(self):
        self.window.api_key.set("secret-test-key")
        self.window.values["semantic_enabled"].set(True)
        self.window.values["model"].set("fixture-model")
        self.window.save_button.invoke()
        settings,key = self.saved.call_args.args
        self.assertEqual(key,"secret-test-key")
        self.assertNotIn("api_key",settings)
        self.assertNotIn("secret-test-key",json.dumps(settings))
        self.assertFalse((self.root/"video_analysis_settings.json").exists())
        self.window.test_button.invoke()
        self.detected.assert_called_once_with(settings,key)
        self.window.values["asr_model"].set("small")
        self.window.download_button.invoke()
        self.downloaded.assert_called_once_with("small")
        self.window.values["model"].set("")
        self.window.set_models(["model-a","model-a","model-b"])
        self.assertEqual(self.window.model_box.cget("values"),("model-a","model-b"))
        self.assertEqual(self.window.values["model"].get(),"model-a")

    def test_readonly_guard_and_invalid_settings_prevent_callbacks(self):
        self.window.can_write = lambda:False
        self.window.save_button.invoke()
        self.window.test_button.invoke()
        self.window.download_button.invoke()
        self.window._open_board(False)
        for callback in (self.saved,self.detected,self.downloaded,self.opened):
            callback.assert_not_called()
        self.assertIn("仍在执行任务",self.window.status.get())
        self.window.can_write = lambda:True
        self.window.values["base_url"].set("http://user:password@example.test")
        self.window.save_button.invoke()
        self.saved.assert_not_called()
        self.assertIn("服务地址",self.window.status.get())

    def test_corrupt_notes_are_visible_without_hiding_good_records(self):
        (self.root/"video_analysis/references/broken.json").write_text("{broken",encoding="utf-8")
        self.window.refresh_notes()
        self.assertEqual(self.window.block_tree.get_children(),("b1","b2"))
        self.assertIn("1 个记录损坏",self.window.summary.get())
        self.assertFalse(self.errors)

    def test_frame_preview_never_reads_paths_outside_project_or_remote_urls(self):
        self.window._frames = [{"path":"../outside.png"}]
        self.window.frame_box.configure(values=("outside",))
        self.window.frame_choice.set("outside")
        with patch("video_analysis_window.Image.open",side_effect=AssertionError("must not read outside")):
            self.window._show_frame()
        self.assertIn("越界",self.window.preview.cget("text"))
        self.window._frames = [{"contentUrl":"https://example.test/frame.png"}]
        self.window._show_frame()
        self.assertIn("尚无本地图片",self.window.preview.cget("text"))

    def test_minimum_layout_keeps_settings_and_results_actions_visible(self):
        self.window.win.geometry("980x720+0+0")
        for tab,controls in ((self.window.settings_tab,(self.window.save_button,self.window.test_button,self.window.download_button)),
                             (self.window.results_tab,(self.window.open_selected_button,self.window.open_all_button,self.window.block_tree))):
            self.window.tabs.select(tab)
            self.tk.update()
            left,top = self.window.win.winfo_rootx(),self.window.win.winfo_rooty()
            for widget in controls:
                with self.subTest(widget=widget):
                    self.assertTrue(widget.winfo_ismapped())
                    self.assertGreaterEqual(widget.winfo_rootx(),left)
                    self.assertGreaterEqual(widget.winfo_rooty(),top)
                    self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),left+self.window.win.winfo_width())
                    self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),top+self.window.win.winfo_height())
        self.assertFalse(self.errors)

    def test_close_and_external_destroy_clear_key_once_without_late_updates(self):
        self.window.api_key.set("temporary-secret")
        self.window.win.destroy()
        self.tk.update()
        self.assertTrue(self.window._closed)
        self.assertEqual(self.window.api_key.get(),"")
        self.closed.assert_called_once_with()
        self.window.set_status("late callback")
        self.window.set_models(["late"])
        self.window.refresh_notes()
        self.window.close()
        self.closed.assert_called_once_with()
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
