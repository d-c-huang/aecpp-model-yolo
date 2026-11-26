"""Real Tk navigation regression checks, isolated from user projects and media."""
from pathlib import Path
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app import Studio
import dataset
from online_jobs import OnlineJobManager


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class AppNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yingge-navigation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"project-one"
        self.other = Path(self.temp.name)/"project-two"
        dataset.initialize(self.root)
        self.video = self.root/"synthetic.mp4"
        self.video.write_bytes(b"synthetic source fixture, never decoded")
        with dataset.connect(self.root) as db:
            for movie_id in (1,2):
                db.execute("INSERT INTO movies(id,digest,name,mode,source_path) VALUES(?,?,?,'time',?)",
                           (movie_id,str(movie_id),f"测试影片 {movie_id}",str(self.video)))
            for frame_id,movie_id in ((11,1),(21,2),(22,2)):
                path = f"frames/{frame_id}.png"
                Image.new("RGB",(160,90),(35,55,75)).save(self.root/path)
                db.execute("INSERT INTO frames(id,movie,slot,ms,path,split,batch_id) VALUES(?,?,?,?,?,'train','batch-one')",
                           (frame_id,movie_id,frame_id,frame_id*1000,path))
        self.managers = []
        self.releases = []
        self.no_network = Mock(side_effect=AssertionError("navigation must not download video"))
        def manager_factory(root):
            manager = OnlineJobManager(root,importer=self.no_network)
            self.managers.append(manager)
            return manager
        self.manager_patch = patch("online_jobs.OnlineJobManager",side_effect=manager_factory)
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
        self.home_patch = patch("app.Path.home",return_value=Path(self.temp.name)/"unused-home")
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.training_patch = patch.object(Studio,"launch_training",side_effect=AssertionError("navigation must not train"))
        self.training_patch.start()
        self.addCleanup(self.training_patch.stop)
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.tk.withdraw()
        self.errors = []
        self.tk.report_callback_exception = lambda cls,exc,tb:self.errors.append(str(exc))
        self.studio = Studio(self.tk)
        self.addCleanup(self.cleanup)
        self.studio.use(self.root)
        self.tk.deiconify()
        self.tk.update()

    def wait_for(self,predicate,timeout=5):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertTrue(predicate())
        self.assertFalse(self.errors)

    def cleanup(self):
        for release in self.releases:
            release.set()
        for manager in self.managers:
            manager.close()
            if manager._thread:
                manager._thread.join(3)
        window = self.studio.cache_window
        if window and not window._closed:
            self.wait_for(lambda:not window.busy)
        library = self.studio.library_page
        if library and library._worker:
            library._worker.join(2)
        indexes = [getattr(page,"usage_index",None) for page in (self.studio.online_window,library)]
        self.studio.close()
        for index in indexes:
            if index and index.worker:
                index.worker.join(2)
        try:
            for timer in self.tk.tk.call("after","info"):
                self.tk.after_cancel(timer)
            self.tk.destroy()
        except tk.TclError:
            pass
        self.no_network.assert_not_called()
        self.assertFalse((Path(self.temp.name)/"unused-home").exists())

    def navigate(self,key):
        self.studio.nav_buttons[key].invoke()
        self.tk.update()
        if key=="cache":
            self.wait_for(lambda:not self.studio.cache_window.busy)
        self.assertEqual(self.studio.active_view,key)
        self.assertFalse(self.errors)

    def completed_job(self):
        manager = self.studio.online_manager
        identifier = manager.add("https://example.test/navigation.mp4",learn=False)["added"][0]
        with manager._db(write=True) as db:
            job = manager._get(db,identifier)
            job.update(status="completed",title="测试影片 2",result={"movie_id":2,"path":str(self.video)},learn=False)
            manager._save(db,job)
        self.studio.online_window._refresh_now()
        self.studio.online_window.tree.selection_set(identifier)
        self.studio.online_window.tree.focus(identifier)
        self.tk.update()
        return identifier

    def test_project_opens_on_embedded_learning_page_without_toplevels(self):
        self.assertEqual(self.studio.active_view,"learn")
        self.assertTrue(self.studio.online_window.embedded)
        self.assertIs(self.studio.online_window.win.master,self.studio.page_area)
        self.assertTrue(self.studio.online_window.win.winfo_ismapped())
        self.assertFalse(any(isinstance(widget,tk.Toplevel) for widget in descendants(self.tk)))

    def test_navigation_preserves_input_selection_pages_and_queue(self):
        identifier = self.completed_job()
        online = self.studio.online_window
        online.links.insert("1.0","https://example.test/draft\n尚未提交的备注")
        online.mode.set("整部验证")
        online.quality.set("1080p · 推荐")
        manager = self.studio.online_manager
        for key in ("library","training","annotation","cache","learn"):
            self.navigate(key)
        self.assertIs(self.studio.online_window,online)
        self.assertIs(self.studio.online_manager,manager)
        self.assertFalse(manager._closed)
        self.assertEqual(online.links.get("1.0","end-1c"),"https://example.test/draft\n尚未提交的备注")
        self.assertEqual(online.tree.selection(),(identifier,))
        self.assertEqual(online.mode.get(),"整部验证")
        self.assertFalse(any(isinstance(widget,tk.Toplevel) for widget in descendants(self.tk)))
        pages = (self.studio.library_page,self.studio.training_window,self.studio.cache_window)
        for key in ("library","training","cache"):
            self.navigate(key)
        self.assertEqual(pages,(self.studio.library_page,self.studio.training_window,self.studio.cache_window))

    def test_page_switch_keeps_an_active_download_running(self):
        manager = self.studio.online_manager
        entered,release = threading.Event(),threading.Event()
        self.releases.append(release)
        stops = []
        def synthetic_importer(root,url,mode,progress,*,stop,**kwargs):
            stops.append(stop)
            entered.set()
            release.wait(6)
            return {"movie_id":2,"path":str(self.video),"title":"测试影片 2"}
        manager._importer = synthetic_importer
        identifier = manager.add("https://example.test/active.mp4",learn=False)["added"][0]
        manager.start()
        self.assertTrue(entered.wait(2))
        for key in ("library","training","annotation","cache","learn"):
            self.navigate(key)
            self.assertTrue(manager.busy)
            self.assertFalse(stops[0].is_set())
            self.assertIs(self.studio.online_manager,manager)
        release.set()
        self.wait_for(lambda:not manager.busy)
        self.assertEqual(next(job for job in manager.list_jobs() if job["id"]==identifier)["status"],"completed")

    def test_open_movie_routes_to_annotation_and_return_preserves_selected_frame(self):
        identifier = self.completed_job()
        online = self.studio.online_window
        online.annotate_button.invoke()
        self.tk.update()
        self.assertEqual(self.studio.active_view,"annotation")
        self.assertEqual(self.studio.movies[self.studio.movie_picker.get()]["id"],2)
        self.studio.tree.selection_set("22")
        self.studio.tree.focus("22")
        self.tk.update()
        self.assertEqual(self.studio.row["id"],22)
        self.navigate("learn")
        self.assertEqual(online.tree.selection(),(identifier,))
        self.navigate("annotation")
        self.assertEqual(self.studio.row["id"],22)
        self.assertEqual(self.studio.tree.selection(),("22",))
        self.navigate("library")
        self.wait_for(lambda:len(self.studio.library_page._movies)==2)
        self.studio.library_page.tree.selection_set("1")
        self.tk.update()
        self.studio.library_page.action_buttons["annotate"].invoke()
        self.assertEqual(self.studio.active_view,"annotation")
        self.assertEqual(self.studio.movies[self.studio.movie_picker.get()]["id"],1)

    def test_project_switch_disposes_old_pages_timers_and_manager(self):
        for key in ("library","training","cache"):
            self.navigate(key)
        old_online = self.studio.online_window
        old_library = self.studio.library_page
        old_training = self.studio.training_window
        old_cache = self.studio.cache_window
        old_manager = self.studio.online_manager
        old_timers = {value for value in (old_online._timer,old_online._sash_timer,old_library._timer,
                                         old_training.timer,old_cache._timer) if value}
        self.studio.use(self.other)
        self.tk.update()
        self.assertEqual(self.studio.root,self.other.resolve())
        self.assertEqual(self.studio.active_view,"learn")
        self.assertTrue(old_manager._closed)
        self.assertIsNot(self.studio.online_manager,old_manager)
        self.assertTrue(old_online._closed)
        self.assertTrue(old_library.closed)
        self.assertTrue(old_training._closed)
        self.assertTrue(old_cache._closed)
        for widget in (old_online.win,old_library.frame,old_training.win,old_cache.win):
            self.assertFalse(widget.winfo_exists())
        self.assertFalse(old_timers & set(self.tk.tk.call("after","info")))
        self.assertIsNone(self.studio.library_page)
        self.assertIsNone(self.studio.training_window)
        self.assertIsNone(self.studio.cache_window)
        self.assertIsNone(self.studio.row)
        self.assertFalse(self.studio.rows)
        self.assertFalse(self.errors)

    def test_control_z_only_undoes_annotation_when_canvas_or_tree_has_focus(self):
        self.studio.undo = Mock()
        online = self.studio.online_window
        online._set_composer_expanded(True)
        online.links.insert("1.0","draft")
        online.links.edit_separator()
        online.links.focus_force()
        self.tk.update()
        online.links.event_generate("<Control-z>")
        self.tk.update()
        self.studio.undo.assert_not_called()
        self.assertEqual(online.links.get("1.0","end-1c"),"")
        self.navigate("annotation")
        self.studio.canvas.focus_force()
        self.tk.update()
        self.studio.canvas.event_generate("<Control-z>")
        self.tk.update()
        self.studio.undo.assert_called_once_with()
        self.studio.undo.reset_mock()
        self.navigate("library")
        self.studio.library_page.detail_text.focus_force()
        self.tk.update()
        self.studio.library_page.detail_text.event_generate("<Control-z>")
        self.tk.update()
        self.studio.undo.assert_not_called()
        self.assertFalse(self.errors)

    def assert_widget_inside(self,widget,bounds):
        left,top,right,bottom = bounds
        self.assertTrue(widget.winfo_ismapped(),str(widget))
        self.assertGreaterEqual(widget.winfo_width(),widget.winfo_reqwidth(),widget.cget("text"))
        self.assertGreaterEqual(widget.winfo_rootx(),left)
        self.assertGreaterEqual(widget.winfo_rooty(),top)
        self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),right)
        self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bottom)

    def check_layout(self,geometry):
        self.tk.geometry(geometry+"+0+0")
        self.tk.update()
        bounds = (self.tk.winfo_rootx(),self.tk.winfo_rooty(),
                  self.tk.winfo_rootx()+self.tk.winfo_width(),self.tk.winfo_rooty()+self.tk.winfo_height())
        for key in ("learn","library","training","annotation","cache"):
            self.navigate(key)
            for name,button in self.studio.nav_buttons.items():
                with self.subTest(size=geometry,page=key,navigation=name):
                    self.assert_widget_inside(button,bounds)
            if key=="learn":
                page = self.studio.online_window
                main = (page.composer_toggle,page.pause_button,page.feed_button,page.annotate_button)
                self.assertGreater(page.tree.winfo_height(),45)
            elif key=="library":
                main = tuple(self.studio.library_page.action_buttons.values())
                self.assertGreater(self.studio.library_page.tree.winfo_height(),45)
            elif key=="training":
                page = self.studio.training_window
                main = (page.result_button,page.log_button,page.export_button,page.effect_button)
            elif key=="annotation":
                page = self.studio.annotation_page
                main = tuple(widget for widget in descendants(page) if widget.winfo_class() in ("TButton","TMenubutton"))
            else:
                main = tuple(self.studio.cache_window.action_buttons)
                self.assertGreater(self.studio.cache_window.tree.winfo_height(),45)
            for widget in main:
                with self.subTest(size=geometry,page=key,button=widget.cget("text")):
                    self.assert_widget_inside(widget,bounds)

    def test_minimum_window_keeps_navigation_and_main_actions_visible(self):
        self.check_layout("980x760")

    def test_default_window_keeps_navigation_and_main_actions_visible(self):
        self.check_layout("1280x900")

    def test_project_transfer_blocks_mutation_and_exit_without_a_cache_page(self):
        self.assertIsNone(self.studio.cache_window)
        self.studio.project_transfer = True
        try:
            self.assertTrue(self.studio.storage_busy())
            original = self.studio.root
            self.studio.use(self.other)
            self.assertEqual(self.studio.root,original)
            self.assertFalse(self.other.exists())
            self.assertFalse(self.studio.show_movie_profiles([1]))
            self.assertIsNone(self.studio.profile_window)
            self.studio.show_cache()
            self.assertIsNone(self.studio.cache_window)
            with patch("training_setup.training_setup") as configure:
                self.studio.start_training()
                configure.assert_not_called()
            self.assertFalse(self.studio.ready())
            self.studio.close()
            self.assertFalse(self.studio.closing)
            self.assertTrue(self.tk.winfo_exists())
            self.assertIn("迁移",self.studio.status.get())
        finally:
            self.studio.project_transfer = False

    def test_export_transfer_flag_clears_on_success_and_failure(self):
        for fail in (False,True):
            with self.subTest(failed=fail):
                entered,release = threading.Event(),threading.Event()
                self.releases.append(release)
                def export(*args,**kwargs):
                    entered.set()
                    release.wait(5)
                    if fail:
                        raise ValueError("synthetic export failure")
                    return {"files":1}
                target = Path(self.temp.name)/"project.zip"
                with patch("portable_project.export_project",side_effect=export), \
                     patch("app.options",return_value={"videos":"包含原片（推荐）"}), \
                     patch("app.filedialog.asksaveasfilename",return_value=str(target)), \
                     patch("app.messagebox.showinfo"):
                    self.studio.export_project()
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(self.studio.project_transfer)
                    self.assertTrue(self.studio.busy)
                    self.assertIn("disabled",self.studio.cancel.state())
                    release.set()
                    self.wait_for(lambda:not self.studio.busy)
                    self.assertFalse(self.studio.project_transfer)
                    self.assertFalse(self.studio.storage_busy())
                    self.assertNotIn("disabled",self.studio.confirm_button.state())
                    self.assertNotIn("disabled",self.studio.delete_button.state())
                self.assertFalse(target.exists())

    def test_import_transfer_releases_guard_before_opening_restored_project(self):
        entered,release = threading.Event(),threading.Event()
        self.releases.append(release)
        def restore(archive,destination,**kwargs):
            entered.set()
            release.wait(5)
            dataset.initialize(destination)
            return {"files":1}
        with patch("portable_project.import_project",side_effect=restore), \
             patch("app.filedialog.askopenfilename",return_value=str(Path(self.temp.name)/"archive.zip")), \
             patch("app.filedialog.askdirectory",return_value=str(self.other)):
            self.studio.import_project()
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.studio.project_transfer)
            release.set()
            self.wait_for(lambda:not self.studio.busy)
        self.assertFalse(self.studio.project_transfer)
        self.assertEqual(self.studio.root,self.other.resolve())
        self.assertEqual(self.studio.active_view,"learn")

    def test_restored_settings_use_relocated_detector_and_sampling_options(self):
        dataset.initialize(self.other)
        detector = self.other/"portable-assets"/"detector.pt"
        detector.parent.mkdir()
        detector.write_bytes(b"synthetic detector, not loaded")
        (self.other/"portable_settings.json").write_text(json.dumps({
            "latest_movie_detector":"portable-assets/detector.pt",
            "latest_movie_settings":{"epochs":45,"sample_fps":2.5,"patience":7,"learning_rate":.002,
                                     "weights":"C:/old-machine/weights.pt","movie_ids":[99]}}),encoding="utf-8")
        self.studio.use(self.other)
        restored = self.studio.movie_training_settings
        self.assertEqual(restored["weights"],str(detector.resolve()))
        self.assertEqual(restored["sample_fps"],2.5)
        self.assertEqual(restored["epochs"],45)
        self.assertEqual(restored["patience"],7)
        self.assertEqual(restored["learning_rate"],.002)
        self.assertIsNone(restored["movie_ids"])
        self.assertEqual(self.studio.movie_defaults()["weights"],str(detector.resolve()))

    def test_missing_detector_uses_movie_learning_without_person_detector(self):
        with patch.object(self.studio,"default_weight",return_value=str(self.root/'missing-detector.pt')):
            self.assertEqual(self.studio.movie_defaults()["weights"],"")

    def test_portable_movie_detector_takes_precedence_over_newer_prediction_detector(self):
        latest = self.root/"runs"/"newer-detector"/"fit"/"weights"/"best.pt"
        latest.parent.mkdir(parents=True)
        latest.write_bytes(b"newer prediction detector, never loaded")
        (self.root/"runs"/"newer-detector"/"completed.json").write_text(
            json.dumps({"kind":"annotations"}),encoding="utf-8")
        original = self.root/"portable-assets"/"original-detector.pt"
        original.parent.mkdir()
        original.write_bytes(b"movie-compatible detector, never loaded")
        for relative,expected in (("",""),("portable-assets/original-detector.pt",str(original.resolve()))):
            with self.subTest(portable_detector=relative):
                (self.root/"portable_settings.json").write_text(json.dumps({
                    "latest_movie_detector":relative,"latest_movie_settings":{"epochs":30}}),encoding="utf-8")
                self.studio.use(self.root)
                self.assertEqual(self.studio.latest_detector(self.root),str(latest))
                self.assertEqual(self.studio.default_weight(self.root),str(latest))
                self.assertEqual(self.studio.movie_defaults()["weights"],expected)
                self.assertEqual(self.studio.movie_training_settings["weights"],expected)


if __name__=="__main__":
    unittest.main()
