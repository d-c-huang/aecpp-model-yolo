from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

import theme
from training_setup import training_setup


class TrainingSetupTests(unittest.TestCase):
    def setUp(self):
        try:
            self.parent = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.parent.withdraw()
        theme.apply_theme(self.parent)
        self.addCleanup(self.parent.destroy)

    def widgets(self, parent):
        for widget in parent.winfo_children():
            yield widget
            yield from self.widgets(widget)

    def run_dialog(self, action, **kwargs):
        failures = []
        def interact():
            win = next(widget for widget in self.parent.winfo_children() if isinstance(widget, tk.Toplevel))
            try:
                action(list(self.widgets(win)))
            except Exception as exc:
                failures.append(exc)
                win.destroy()
        self.parent.after(20, interact)
        with patch("training_setup.messagebox.showerror", side_effect=AssertionError("unexpected validation error")):
            result = training_setup(self.parent, kwargs.pop("movies", []), kwargs.pop("current", None),
                                    kwargs.pop("weights", ""), **kwargs)
        if failures:
            raise failures[0]
        return result

    @staticmethod
    def click(widgets, title):
        next(widget for widget in widgets if isinstance(widget, ttk.Button) and widget.cget("text") == title).invoke()

    def test_movie_defaults_and_presets_can_be_saved_before_importing_a_movie(self):
        result = self.run_dialog(lambda widgets: self.click(widgets, "保存学习设置"), movie_only=True)
        self.assertEqual(result["epochs"], 30)
        self.assertEqual(result["patience"], 5)
        self.assertEqual(result["learning_rate"], .001)
        self.assertFalse(result["restart"])
        def choose(widgets):
            preset = next(widget for widget in widgets if isinstance(widget, ttk.Combobox)
                          and "深入学习 · 80 轮" in widget.cget("values"))
            preset.set("深入学习 · 80 轮")
            preset.event_generate("<<ComboboxSelected>>")
            self.click(widgets, "保存学习设置")
        result = self.run_dialog(choose, movie_only=True)
        self.assertEqual((result["epochs"], result["patience"]), (80, 8))

    def test_saved_settings_roundtrip_and_detector_still_defaults_to_five_epochs(self):
        initial = {"epochs": 42, "patience": 7, "learning_rate": .0002, "restart": True,
                   "sample_fps": .5, "device": "cpu", "weights": "", "movie_ids": [4]}
        result = self.run_dialog(lambda widgets: self.click(widgets, "保存学习设置"), movie_only=True,
                                 movies=[{"id": 4}], current=4, initial=initial)
        for key, value in initial.items():
            self.assertEqual(result[key], value)
        with tempfile.TemporaryDirectory() as temporary:
            weight = Path(temporary) / "local.pt"
            weight.touch()
            def switch(widgets):
                task = next(widget for widget in widgets if isinstance(widget, ttk.Combobox)
                            and "截图检测与台词检索" in widget.cget("values"))
                task.set("截图检测与台词检索")
                task.event_generate("<<ComboboxSelected>>")
                self.click(widgets, "开始后台训练")
            result = self.run_dialog(switch, weights=str(weight))
        self.assertEqual(result["kind"], "annotations")
        self.assertEqual(result["epochs"], 5)


if __name__ == "__main__":
    unittest.main()
