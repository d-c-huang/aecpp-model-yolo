"""Real Tk checks for visual evidence and the opt-in vision controls."""
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
from video_visual_learning import save_visual_reference
from video_analysis_window import VideoAnalysisWindow


class VideoVisualWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/"project"
        dataset.initialize(self.root)
        source = Path(self.temp.name)/"visual-fixture.bin"
        source.write_bytes(b"fixture source for evidence-viewer checks")
        self.movie = dict(dataset.register_movie(self.root,source,"time",lambda _:None))
        frames = []
        for index in range(3):
            path = self.root/f"video_analysis/visual_frames/frame-{index}.jpg"
            path.parent.mkdir(parents=True,exist_ok=True)
            Image.new("RGB",(320,180),(index*70,80,100)).save(path)
            frames.append({"time_ms":1000+index*1000,"path":path.relative_to(self.root).as_posix()})
        self.note = save_visual_reference(self.root,{"version":1,"movie_id":self.movie["id"],
            "film_hash":self.movie["digest"],"model":"vision-fixture","provider":"ollama",
            "clips":[{"id":"clip-1","start_ms":1000,"end_ms":4000,"text":"人物举起双手，身体向后移动。",
                "context_text":"字幕原文保持原样","action":"shake","emotion":"shocked","camera":"close",
                "confidence":.87,"evidence":"三帧中手臂位置改变，眼睛张大。","frames":frames,"cut_times_ms":[2500]}]})
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.temp.cleanup()
            self.skipTest(str(exc))
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.errors = []
        self.tk.report_callback_exception = lambda cls,exc,tb:self.errors.append(str(exc))
        self.saved,self.downloaded,self.detected,self.closed = [Mock() for _ in range(4)]
        self.window = VideoAnalysisWindow(self.tk,self.root,movie_id=self.movie["id"],
            on_save=self.saved,on_download_vision=self.downloaded,on_test_models=self.detected,on_close=self.closed)
        self.tk.update()

    def tearDown(self):
        self.window.close()
        self.tk.report_callback_exception = None
        self.tk.destroy()
        self.window = self.tk = None
        gc.collect()
        self.temp.cleanup()

    def test_visual_only_movie_shows_three_real_frames_and_unreviewed_evidence(self):
        window = self.window
        self.assertEqual(window._movie_ids[window.movie_filter.get()],self.movie["id"])
        self.assertEqual(window.visual_tree.get_children(),("clip-1",))
        self.assertEqual(window.visual_tree.item("clip-1","values"),("00:00:01","抖动","震惊","近景","87%"))
        self.assertEqual(len(window.visual_frame_box.cget("values")),3)
        self.assertIsNotNone(window._visual_photo)
        detail = window.visual_detail.get("1.0","end")
        for value in ("字幕原文保持原样","三帧中手臂位置改变","待审核","切镜候选","vision-fixture",self.note["path"]):
            self.assertIn(value,detail)
        self.assertEqual(window.visual_detail.cget("state"),"disabled")
        self.assertIn("非逐帧覆盖",window.visual_summary.get())
        for index in (1,2):
            window.visual_frame_box.current(index)
            window._show_visual_frame()
            self.assertIsNotNone(window._visual_photo)
        self.downloaded.assert_not_called()
        self.detected.assert_not_called()

    def test_vision_settings_and_download_callback_are_explicit_and_typed(self):
        window = self.window
        window.values["visual_enabled"].set(True)
        window.values["visual_model"].set("vision-fixture")
        window.values["visual_max_clips"].set(7)
        window.api_key.set("ephemeral-key")
        window.save_button.invoke()
        settings,key = self.saved.call_args.args
        self.assertTrue(settings["visual_enabled"])
        self.assertEqual(settings["visual_model"],"vision-fixture")
        self.assertEqual(settings["visual_max_clips"],7)
        self.assertIs(type(settings["visual_max_clips"]),int)
        self.assertEqual(key,"ephemeral-key")
        self.assertNotIn(key,json.dumps(settings))
        window.vision_download_button.invoke()
        self.downloaded.assert_called_once_with(settings,key)
        window.set_models(["text-fixture","vision-fixture"])
        self.assertEqual(window.visual_model_box.cget("values"),("text-fixture","vision-fixture"))
        self.assertEqual(window.values["visual_model"].get(),"vision-fixture")

    def test_no_automatic_visual_selection_and_invalid_budget_blocks_actions(self):
        window = self.window
        window.set_models(["text-only"])
        self.assertEqual(window.values["visual_model"].get(),"")
        window.values["visual_max_clips"].set(61)
        window.save_button.invoke()
        window.vision_download_button.invoke()
        self.saved.assert_not_called()
        self.downloaded.assert_not_called()
        self.assertIn("1–60",window.status.get())
        window.values["visual_max_clips"].set(12)
        window.can_write = lambda:False
        window.vision_download_button.invoke()
        self.downloaded.assert_not_called()
        self.assertFalse(self.errors)

    def test_visual_preview_rejects_outside_path(self):
        window = self.window
        window._visual_frames = [{"time_ms":1000,"path":"../outside.jpg"}]
        window.visual_frame_box.configure(values=("outside",))
        window.visual_frame_box.current(0)
        with patch("video_analysis_window.Image.open",side_effect=AssertionError("must not read outside")):
            window._show_visual_frame()
        self.assertIsNone(window._visual_photo)
        self.assertIn("越界",window.visual_preview.cget("text"))

    def test_minimum_layout_includes_vision_settings_and_evidence(self):
        window = self.window
        window.win.geometry("980x720+0+0")
        for tab,widgets in ((window.settings_tab,(window.visual_model_box,window.vision_download_button,window.save_button,window.download_button)),
                            (window.visual_tab,(window.visual_tree,window.visual_preview,window.visual_detail))):
            window.tabs.select(tab)
            self.tk.update()
            right = window.win.winfo_rootx()+window.win.winfo_width()
            bottom = window.win.winfo_rooty()+window.win.winfo_height()
            for widget in widgets:
                self.assertTrue(widget.winfo_ismapped())
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),right)
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bottom)
        self.assertFalse(self.errors)

    def test_refresh_select_and_destroy_clear_visual_state(self):
        window = self.window
        window.select_movie(None)
        self.assertEqual(window.movie_filter.get(),"全部影片")
        window.select_movie(self.movie["id"])
        self.assertEqual(window.visual_tree.get_children(),("clip-1",))
        window.win.destroy()
        self.tk.update()
        self.assertIsNone(window._visual_photo)
        window.refresh_notes()
        window.set_models(["late"])
        self.closed.assert_called_once_with()
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
