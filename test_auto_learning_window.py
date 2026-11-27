"""UI lifecycle and routing checks with local disposable project data."""
from copy import deepcopy
import gc
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock

import dataset
import theme
from auto_learning_window import AutoLearningWindow, available_movies


class FakeManager:
    def __init__(self, root):
        self.root, self.jobs, self.calls = root, [], []
        self.busy, self.paused = False, False

    def list_jobs(self):
        return deepcopy(self.jobs)

    def enqueue(self, ids, *, force=False):
        self.calls.append(("enqueue", ids, force))
        return [str(value) for value in ids]

    def pause(self):
        self.calls.append(("pause",))
        self.paused = True

    def resume(self):
        self.calls.append(("resume",))
        self.paused = False

    def retry(self, ids):
        self.calls.append(("retry", ids))

    def cancel(self, ids):
        self.calls.append(("cancel", ids))


class AutoLearningWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory(prefix="auto-learning-window-")
        self.root = Path(self.temporary.name)
        dataset.initialize(self.root)
        self.video = self.root / "movie.mp4"
        self.video.write_bytes(b"local fixture")
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path) VALUES(1,'one','对白示例','train',?)", (str(self.video),))
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path) VALUES(2,'gone','缺失原片','val',?)", (str(self.root / "gone.mp4"),))
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.temporary.cleanup()
            self.skipTest(str(exc))
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.manager = FakeManager(self.root)
        self.open_board, self.closed = Mock(), Mock()
        self.window = AutoLearningWindow(self.tk, self.manager, on_open_board=self.open_board, on_close=self.closed)

    def tearDown(self):
        self.window.close()
        self.tk.report_callback_exception = None
        try:
            self.tk.destroy()
        except tk.TclError:
            pass
        self.window = self.tk = None
        gc.collect()
        self.temporary.cleanup()

    def select(self, *ids):
        self.window.refresh_now()
        self.window.tree.selection_set(ids)
        self.window._selection_changed()

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from AutoLearningWindowTests.descendants(child)

    def test_open_board_parts_rechecks_paths_and_selection(self):
        folder = self.root / "auto_learning"
        folder.mkdir()
        paths = [folder / f"part-{i}.json" for i in range(2)]
        for path in paths:
            path.write_text("{}", encoding="utf-8")
        self.manager.jobs = [{"id": "done", "movie_id": 1, "status": "completed", "title": "对白示例",
                              "board_paths": [str(path.relative_to(self.root)) for path in paths], "example_count": 20}]
        self.select("done")
        self.assertEqual(self.window.board_part_box.cget("values"), ("第 1 / 2 份", "第 2 / 2 份"))
        self.window.board_part_box.current(1)
        self.window._open_board()
        self.open_board.assert_called_once_with(paths[1])
        paths[1].unlink()
        self.window._open_board()
        self.open_board.assert_called_once()
        self.manager.jobs[0]["board_paths"] = [str(Path(__file__).resolve())]
        self.select("done")
        self.assertEqual(str(self.window.open_button.cget("state")), "disabled")

    def test_pause_retry_cancel_and_force_reanalysis_route_to_manager(self):
        self.manager.jobs = [{"id": "bad", "movie_id": 1, "status": "failed"},
                              {"id": "run", "movie_id": 2, "status": "running"}]
        self.select("bad", "run")
        self.window._retry()
        self.window._reanalyze()
        self.window._cancel()
        self.assertIn(("retry", ["bad"]), self.manager.calls)
        self.assertIn(("enqueue", [1], True), self.manager.calls)
        self.assertIn(("cancel", ["run"]), self.manager.calls)
        self.window.can_write = lambda: False
        count = len(self.manager.calls)
        self.window._retry()
        self.assertEqual(len(self.manager.calls), count)
        self.window._toggle_pause()
        self.assertTrue(self.manager.paused)
        self.window._toggle_pause()
        self.assertTrue(self.manager.paused)
        self.window.can_write = lambda: True
        self.window._toggle_pause()
        self.assertFalse(self.manager.paused)

    def test_needs_input_explains_next_step_and_search(self):
        self.manager.jobs = [{"id": "missing", "movie_id": 1, "title": "无字幕视频", "status": "needs_input",
                              "message": "没有可用定时字幕"}, {"id": "done", "movie_id": 2, "title": "完成", "status": "completed"}]
        self.select("missing")
        self.assertIn("带时间码", self.window.detail.get())
        self.window.search.set("无字幕")
        self.assertEqual(self.window.tree.get_children(), ("missing",))
        self.assertIn("1 个需处理", self.window.summary.get())

    def test_helper_import_binds_one_inactive_task_and_checks_write_guard(self):
        self.window.close()
        callback = Mock()
        self.window = AutoLearningWindow(self.tk, self.manager, on_open_board=self.open_board, on_import_helper=callback)
        self.manager.jobs = [{"id": "missing", "movie_id": 1, "status": "needs_input"},
                             {"id": "done", "movie_id": 2, "status": "completed"}]
        self.select("missing")
        self.assertFalse(self.window.import_helper_button.instate(["disabled"]))
        self.window.import_helper_button.invoke()
        callback.assert_called_once_with(1)
        callback.reset_mock()
        for status in ("running", "queued", "paused"):
            self.manager.jobs[0]["status"] = status
            self.select("missing")
            self.assertTrue(self.window.import_helper_button.instate(["disabled"]))
            self.window._import_helper()
        self.manager.jobs[0]["status"] = "needs_input"
        self.select("missing", "done")
        self.window._import_helper()
        self.select("missing")
        self.window.can_write = lambda: False
        self.window._import_helper()
        callback.assert_not_called()
        self.window.win.geometry("860x560")
        self.tk.update()
        for widget in (self.window.import_helper_button, self.window.add_button, self.window.tree):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(), self.window.win.winfo_rootx()+860)
        self.assertGreater(self.window.tree.winfo_height(), 100)

    def test_existing_movie_picker_filters_missing_sources_and_enqueues_selection(self):
        self.assertEqual([row["id"] for row in available_movies(self.root)], [1])
        self.window._choose_movies()
        picker = self.window._picker
        self.window._choose_movies()
        self.assertIs(self.window._picker, picker)
        tree = next(w for w in self.descendants(picker) if w.winfo_class() == "Treeview")
        tree.selection_set("1")
        button = next(w for w in self.descendants(picker) if w.winfo_class() == "TButton")
        button.invoke()
        self.assertIn(("enqueue", [1], False), self.manager.calls)
        self.assertFalse(picker.winfo_exists())

    def test_minimum_size_shows_actions_and_closing_keeps_workers_alive(self):
        self.window.win.geometry("860x560")
        self.manager.busy = True
        self.window.refresh_now()
        self.tk.update()
        for widget in (self.window.tree, self.window.add_button, self.window.open_button, self.window.cancel_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.window.win.winfo_rooty() + 560)
        self.assertGreater(self.window.tree.winfo_height(), 100)
        self.window.close()
        self.assertTrue(self.manager.busy)
        self.assertIsNone(self.window._timer)
        self.assertEqual(self.window.search.trace_info(), [])
        self.window._render = Mock(side_effect=AssertionError("Closed view received a trace callback"))
        self.window.search.set("closed")
        self.window._render.assert_not_called()
        self.closed.assert_called_once()

    def test_external_destroy_cleans_polling_without_callbacks(self):
        errors = []
        self.tk.report_callback_exception = lambda *args: errors.append(args)
        self.window.win.destroy()
        self.assertTrue(self.window._closed)
        self.assertIsNone(self.window._timer)
        self.closed.assert_called_once()
        self.assertFalse(errors)

    def test_model_summary_uses_real_reference_counts_and_validation(self):
        from auto_learning import build_reference
        model = build_reference([{"version": 1, "film_hash": "sample", "movie_id": 1, "mode": "train",
                                  "timing_examples": [{"text": "字幕时长参考", "start_ms": i*4000,
                                                       "end_ms": i*4000+3000, "duration": 3.} for i in range(3)]}])
        folder = self.root / "auto_learning"
        folder.mkdir()
        (folder / "reference.json").write_text(json.dumps(model), encoding="utf-8")
        self.window.refresh_now()
        self.assertIn("3 条样例 / 1 部训练影片", self.window.summary.get())
        self.assertIn("尚未独立验证", self.window.summary.get())
        self.manager.jobs = [{"id": "failed", "movie_id": 1, "title": "待修复", "status": "failed"}]
        (folder / "reference.json").write_text("broken", encoding="utf-8")
        self.window.refresh_now()
        self.assertIn("节奏参考不可用", self.window.summary.get())
        self.assertEqual(self.window.tree.get_children(), ("failed",))


if __name__ == "__main__":
    unittest.main()
