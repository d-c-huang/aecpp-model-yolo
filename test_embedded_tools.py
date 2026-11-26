"""Real Tk checks for training/cache pages mounted inside the main window."""
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock

from cache_window import CacheWindow
import dataset
import theme
from training_window import TrainingWindow, save_job


class EmbeddedToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yingge-embedded-tools-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)/"project"
        dataset.initialize(self.project)
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.tk.title("主应用")
        self.tk.geometry("900x550+0+0")
        self.tk.minsize(640,400)
        theme.apply_theme(self.tk)
        self.tk.protocol("WM_DELETE_WINDOW",lambda:None)
        self.tk.bind("<Escape>",lambda _:None)
        self.container = ttk.Frame(self.tk)
        self.container.pack(fill="both",expand=True)
        self.status = tk.StringVar(self.tk,value="训练进行中")
        self.queued = tk.StringVar(self.tk,value="新增标注将在下一批投喂")
        self.active = Mock(return_value="run-1")
        self.errors = []
        self.tk.report_callback_exception = lambda cls,exc,tb:self.errors.append(str(exc))
        self.pages = []
        self.tk.update()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for page in self.pages:
            if isinstance(page,CacheWindow) and not page._closed:
                self.wait_for(lambda:not page.busy)
            page.close()
        self.tk.destroy()

    def wait_for(self,predicate,timeout=3):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            self.tk.update()
            time.sleep(.01)
        self.tk.update()
        self.assertTrue(predicate())
        self.assertFalse(self.errors)

    def training(self):
        page = TrainingWindow(self.tk,self.project,self.active,self.status,self.queued,container=self.container)
        self.pages.append(page)
        return page

    def cache(self,changed=None):
        page = CacheWindow(self.tk,self.project,is_busy=lambda:False,on_changed=changed,container=self.container)
        self.pages.append(page)
        self.wait_for(lambda:not page.busy)
        return page

    def assert_inside(self,widget,bounds):
        self.assertTrue(widget.winfo_ismapped(),str(widget))
        self.assertGreater(widget.winfo_width(),1)
        self.assertGreater(widget.winfo_height(),1)
        self.assertGreaterEqual(widget.winfo_rootx(),bounds.winfo_rootx())
        self.assertGreaterEqual(widget.winfo_rooty(),bounds.winfo_rooty())
        self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),bounds.winfo_rootx()+bounds.winfo_width())
        self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bounds.winfo_rooty()+bounds.winfo_height())

    def test_embedded_constructor_and_close_leave_main_window_intact(self):
        original = (self.tk.title(),self.tk.geometry(),self.tk.minsize(),
                    self.tk.protocol("WM_DELETE_WINDOW"),self.tk.bind("<Escape>"))
        for factory in (self.training,self.cache):
            page = factory()
            self.assertIsInstance(page.win,ttk.Frame)
            self.assertFalse(page.win.winfo_ismapped())
            page.win.pack(fill="both",expand=True)
            page.show()
            self.tk.update()
            timer = page.timer if isinstance(page,TrainingWindow) else page._timer
            self.assertTrue(page.close())
            self.assertTrue(self.tk.winfo_exists())
            self.assertFalse(page.win.winfo_exists())
            self.assertNotIn(timer,self.tk.tk.call("after","info"))
            self.assertEqual(original,(self.tk.title(),self.tk.geometry(),self.tk.minsize(),
                                      self.tk.protocol("WM_DELETE_WINDOW"),self.tk.bind("<Escape>")))

    def test_hidden_training_page_keeps_polling_and_selection(self):
        save_job(self.project,"run-1",{"kind":"movie-temporal","status":"running",
                  "started":"2026-11-26T10:00:00","epochs":30})
        page = self.training()
        page.win.pack(fill="both",expand=True)
        self.tk.update()
        self.assertEqual(page.history.selection(),("run-1",))
        page.win.pack_forget()
        save_job(self.project,"run-1",{"kind":"movie-temporal","status":"completed",
                  "started":"2026-11-26T10:00:00","epochs":30})
        self.status.set("训练完成 · run-1")
        self.wait_for(lambda:page.records["run-1"]["status"]=="completed")
        page.win.pack(fill="both",expand=True)
        page.show()
        self.tk.update()
        self.assertEqual(page.history.selection(),("run-1",))
        self.assertIn("已完成",page.summary.get())
        self.assertIsNotNone(page.timer)

    def test_hidden_cache_page_finishes_work_and_still_rejects_busy_close(self):
        changed = Mock()
        page = self.cache(changed)
        page.win.pack(fill="both",expand=True)
        self.tk.update()
        page.win.pack_forget()
        page._run(lambda report:None,changed=True)
        self.assertFalse(page.close())
        self.assertTrue(page.win.winfo_exists())
        self.wait_for(lambda:not page.busy)
        changed.assert_called_once_with()
        page.win.pack(fill="both",expand=True)
        page.show()
        self.tk.update()
        self.assertInsideCache(page)

    def assertInsideCache(self,page):
        for button in page.action_buttons:
            self.assert_inside(button,page.win)
        self.assert_inside(page.tree,page.win)

    def test_training_actions_survive_dense_results_at_900_by_550(self):
        run = self.project/"runs"/"run-1"
        run.mkdir(parents=True)
        complete = {"kind":"movie-temporal","epochs":30,"actual_epochs":18,"best_epoch":16,
                    "sample_fps":2,"patience":5,"profile_conditioned":True,"profile_count":3,
                    "movie_count":3,"sample_count":12000,"train_windows":9500,"val_windows":2500,
                    "validation_kind":"within-film","sampling_strategy":"film-balanced",
                    "train_loss":.02,"val_loss":.03,"baseline_loss":.06,"restart":True,
                    "warm_start_reason":"使用上一版模型继续学习，旧训练记录与人工标注已经保留。"}
        (run/"completed.json").write_text(json.dumps(complete),encoding="utf-8")
        (run/"movie_temporal.pt").touch()
        (self.project/"logs").mkdir(exist_ok=True)
        (self.project/"logs"/"run-1-train.log").write_text("测试训练日志\n"*30,encoding="utf-8")
        page = self.training()
        page.win.pack(fill="both",expand=True)
        self.tk.update()
        for height in (550,600):
            self.tk.geometry(f"900x{height}+0+0")
            self.tk.update()
            for button in (page.result_button,page.log_button,page.export_button,page.effect_button):
                with self.subTest(height=height,button=button.cget("text")):
                    self.assert_inside(button,page.win)
                    self.assertEqual(str(button.cget("state")),"normal")
            self.assert_inside(page.log,page.win)
            self.assertGreater(page.log.winfo_height(),30)
        self.assertIn("训练损失",page.overview.get("1.0","end"))
        self.assertIn("演员、角色和剧情",page.overview.get("1.0","end"))

    def test_cache_actions_survive_long_status_at_900_by_550(self):
        page = self.cache()
        page.win.pack(fill="both",expand=True)
        page.detail.set("缓存路径/"*120)
        page.status.set("迁移已校验，已完成的标注和模型保留。"*80)
        self.tk.update()
        self.assertInsideCache(page)
        self.assert_inside(page.status_text,page.win)
        self.assertGreater(page.tree.winfo_height(),60)

    def test_destroying_container_cleans_page_and_effect_timers(self):
        run = self.project/"runs"/"run-1"
        run.mkdir(parents=True)
        (run/"completed.json").write_text(json.dumps({"kind":"movie-temporal"}),encoding="utf-8")
        (run/"movie_evaluation.json").write_text(json.dumps({"kind":"movie-temporal-evaluation",
            "model_loss":.01,"baseline_loss":.02,"sample_count":10}),encoding="utf-8")
        page = self.training()
        page.win.pack(fill="both",expand=True)
        page.show_effect()
        effect = page.effect_windows[run]
        cache = self.cache()
        timers = (page.timer,effect.timer,cache._timer)
        self.container.destroy()
        self.tk.update()
        self.assertTrue(self.tk.winfo_exists())
        self.assertTrue(page._closed)
        self.assertTrue(cache._closed)
        self.assertTrue(effect.closed.is_set())
        self.assertIsNone(page.timer)
        self.assertIsNone(effect.timer)
        self.assertIsNone(cache._timer)
        for timer in timers:
            self.assertNotIn(timer,self.tk.tk.call("after","info"))
        page.refresh()
        cache._poll()
        effect.poll()
        self.assertFalse(self.errors)

    def test_training_standalone_api_remains_available(self):
        page = TrainingWindow(self.tk,self.project,self.active,self.status,self.queued)
        self.pages.append(page)
        self.assertIsInstance(page.win,tk.Toplevel)
        self.assertEqual(page.win.title(),"训练记录")
        page.win.withdraw()
        page.show()
        self.tk.update()
        self.assertEqual(page.win.state(),"normal")
        page.close()
        self.assertTrue(self.tk.winfo_exists())


if __name__=="__main__":
    unittest.main()
