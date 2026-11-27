"""Resource binding UI and strict primary-export routing, using real Tk."""
import copy
import gc
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


class StoryResourcesWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/"project"
        self.root.mkdir()
        self.assets = Path(self.temp.name)/"assets"
        self.assets.mkdir()
        self.catalog = {"version":1,"root":str(self.assets),"actors":[{
            "id":"actors/hero.actor","name":"小明","path":str(self.assets/"actors/hero.actor"),
            "relative_path":"actors/hero.actor","sha256":"fixture","dependencies":[],"valid":True,"issues":[],
            "actions":[{"id":"idle-real","name":"站立","loop":True,"duration_frames":30},
                       {"id":"talk-real","name":"说话","loop":True,"duration_frames":30},
                       {"id":"wobble-real","name":"摇摆","loop":False,"duration_frames":30}]}],
            "backgrounds":[{"id":"backgrounds/room.png","name":"客厅","path":str(self.assets/"backgrounds/room.png"),
                            "relative_path":"backgrounds/room.png","sha256":"fixture"}],
            "expressions":[{"id":"expression-neutral","name":"平静","path":str(self.assets/"neutral.png"),"builtin":False,"sha256":"fixture"}],"issues":[]}
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.temp.cleanup()
            self.skipTest(str(exc))
        theme.apply_theme(self.tk)
        self.tk.geometry("980x650+0+0")
        self.host = ttk.Frame(self.tk)
        self.host.pack(fill="both",expand=True)
        self.errors = []
        self.tk.report_callback_exception = lambda cls,exc,tb:self.errors.append(str(exc))
        self.messages = []
        self.message_patch = patch("story_window.messagebox.showerror",side_effect=lambda *args,**kwargs:self.messages.append(args))
        self.message_patch.start()
        self.can_write = Mock(return_value=True)
        self.window = StoryWindow(self.tk,self.root,container=self.host,can_write=self.can_write)
        self.window.win.pack(fill="both",expand=True)
        self.window.board = story_pipeline.generate_storyboard("场景：客厅\n小明：我先说一句话。",source_format="script")
        self.cid = self.window.board["characters"][0]["id"]
        self.window.resource_config = {"version":1,"resource_root":str(self.assets),
            "characters":{self.cid:"actors/hero.actor"},"backgrounds":{"客厅":"backgrounds/room.png"},
            "actions":{self.cid:{"talk":"talk-real","idle":"idle-real"}},"emotions":{self.cid:{"neutral":"expression-neutral"}}}
        self.window.resource_root.set(str(self.assets))
        self.window._render()
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
        self.window = self.tk = self.host = None
        gc.collect()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic()+8
        while self.window.busy and time.monotonic()<deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.errors)

    def scan(self):
        main = threading.get_ident()
        def work(path,report):
            self.assertNotEqual(threading.get_ident(),main)
            self.assertEqual(path,str(self.assets))
            report("正在扫描真实素材路径")
            return copy.deepcopy(self.catalog)
        with patch("aecpp_resources.scan_resources",side_effect=work):
            self.assertTrue(self.window._scan_resources())
            self.wait_idle()
        self.assertFalse(self.messages)

    def test_scan_maps_existing_assets_and_settings_survive_reopen(self):
        from aecpp_resources import load_bindings
        self.scan()
        self.assertEqual(self.window.inventory_tree.get_children(),("actors","backgrounds","expressions"))
        self.assertEqual(set(row["kind"] for row in self.window._resource_rows.values()),{"characters","actions","emotions","backgrounds"})
        self.assertTrue(self.window._check_resources())
        self.assertTrue(self.window._save_resources())
        saved = load_bindings(self.root)
        self.assertEqual(saved["characters"][self.cid],"actors/hero.actor")
        self.assertEqual(saved["actions"][self.cid]["talk"],"talk-real")
        self.assertTrue(self.window.close())
        self.window = StoryWindow(self.tk,self.root,container=self.host,can_write=self.can_write)
        self.window.win.pack(fill="both",expand=True)
        self.assertEqual(self.window.resource_root.get(),str(self.assets))
        self.assertEqual(self.window.resource_config["characters"][self.cid],"actors/hero.actor")
        self.assertIsNone(self.window.resource_catalog,"opening the page must not scan the filesystem automatically")

    def test_missing_action_can_be_explicitly_bound_to_an_existing_action(self):
        self.scan()
        self.window.board["shots"][0]["action"] = "shake"
        self.window.resource_config["actions"][self.cid]["shake"] = "missing-action"
        self.window._render()
        self.assertFalse(self.window._check_resources())
        selected = next(key for key,row in self.window._resource_rows.items() if row["kind"]=="actions" and row["field"]=="shake")
        self.window.resource_tree.selection_set(selected)
        self.window._select_resource_mapping()
        label = next(key for key,value in self.window._resource_options.items() if value=="wobble-real")
        self.window.resource_choice.set(label)
        self.assertTrue(self.window._apply_resource_mapping())
        self.assertEqual(self.window.resource_config["actions"][self.cid]["shake"],"wobble-real")
        self.assertTrue(self.window._check_resources())

    def test_primary_export_requires_resources_and_never_calls_shape_export(self):
        self.assertIn("现有素材",self.window.export_button.cget("text"))
        self.assertIn("示意草稿",self.window.draft_export_button.cget("text"))
        with patch("story_window.filedialog.asksaveasfilename") as choose,patch("aecpp_export.export_project") as shapes:
            self.assertFalse(self.window._export_resources())
        choose.assert_not_called()
        shapes.assert_not_called()
        self.assertEqual(self.window.tabs.index("current"),2)
        self.assertIn("扫描",self.window.status.get())

    def test_primary_export_passes_plain_snapshots_to_resource_exporter(self):
        self.scan()
        output = self.root/"existing-assets.aecpp"
        main = threading.get_ident()
        captured = []
        def export(board,target,*,catalog,bindings,report):
            self.assertNotEqual(threading.get_ident(),main)
            self.assertIsNot(board,self.window.board)
            self.assertIsNot(catalog,self.window.resource_catalog)
            self.assertEqual(bindings["actions"][self.cid]["talk"],"talk-real")
            self.assertEqual(target,output)
            captured.append((board,catalog,bindings))
            target.write_text("fixture-export",encoding="utf-8")
            return target
        with patch("story_window.filedialog.asksaveasfilename",return_value=str(output)),patch("aecpp_resource_export.export_resource_project",side_effect=export),patch("aecpp_export.export_project") as shapes:
            self.assertTrue(self.window._export_resources())
            self.wait_idle()
        self.assertEqual(len(captured),1)
        shapes.assert_not_called()
        self.assertIn("现有素材动画已组装",self.window.status.get())
        self.assertFalse(self.messages)

    def test_busy_and_storage_guards_block_mapping_and_export(self):
        self.can_write.return_value = False
        with patch("aecpp_resources.scan_resources") as scan,patch("aecpp_resources.save_bindings") as save:
            self.assertFalse(self.window._scan_resources())
            self.assertFalse(self.window._save_resources())
            self.assertFalse(self.window._export_resources())
        scan.assert_not_called()
        save.assert_not_called()
        self.can_write.return_value = True
        release = threading.Event()
        def scan(path,report):
            release.wait(5)
            return copy.deepcopy(self.catalog)
        try:
            with patch("aecpp_resources.scan_resources",side_effect=scan):
                self.window._scan_resources()
                self.assertEqual(str(self.window.resource_box.cget("state")),"disabled")
                self.assertFalse(self.window._apply_resource_mapping())
                self.assertFalse(self.window.close())
                release.set()
                self.wait_idle()
        finally:
            release.set()

    def test_resources_layout_keeps_mapping_and_primary_actions_visible(self):
        self.scan()
        self.window.tabs.select(self.window.resources_page)
        self.tk.geometry("980x580+0+0")
        self.tk.update()
        right = self.window.win.winfo_rootx()+self.window.win.winfo_width()
        bottom = self.window.win.winfo_rooty()+self.window.win.winfo_height()
        for widget in (self.window.resource_tree,self.window.resource_box,self.window.save_resources_button,self.window.check_resources_button,self.window.export_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),right)
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),bottom)
        self.assertGreater(self.window.resource_tree.winfo_height(),65)


if __name__ == "__main__":
    unittest.main()
