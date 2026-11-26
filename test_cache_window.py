"""Cache management UI tests avoid touching user resources."""
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import cache_storage as storage
from cache_window import CacheWindow
import dataset
import theme


class CacheWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yingge-cache-window-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"project"
        dataset.initialize(self.root)
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.errors = []
        self.tk.report_callback_exception = lambda cls,exc,tb:self.errors.append(str(exc))
        self.changed = Mock()
        self.other_busy = Mock(return_value=False)
        self.window = CacheWindow(self.tk,self.root,is_busy=self.other_busy,on_changed=self.changed)
        self.addCleanup(self.cleanup)
        self.wait_idle()

    def wait_idle(self):
        deadline = time.monotonic()+5
        while self.window.busy and time.monotonic()<deadline:
            self.tk.update()
            time.sleep(.01)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.errors)

    def cleanup(self):
        self.wait_idle()
        self.window.close()
        self.tk.destroy()

    def test_switch_location_updates_ui_and_notifies_main_window(self):
        target = Path(self.temp.name)/"new cache"
        with patch("cache_window.filedialog.askdirectory",return_value=str(target)), \
             patch("cache_window.messagebox.askyesnocancel",return_value=False):
            self.window._change()
        self.wait_idle()
        self.assertEqual(self.window.location.get(),str(target))
        self.assertEqual(storage.get_settings(self.root)["current"],str(target))
        self.changed.assert_called_once_with()

    def test_active_learning_prevents_destructive_actions(self):
        self.other_busy.return_value = True
        with patch("cache_window.filedialog.askdirectory") as directory, \
             patch("cache_window.messagebox.askyesno") as confirm:
            self.window._change()
            self.window._cleanup()
            self.window._delete()
        directory.assert_not_called()
        confirm.assert_not_called()
        self.assertIn("下载、截图或训练",self.window.status.get())

    def test_close_waits_for_inflight_storage_operation(self):
        self.window.busy = True
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.win.winfo_exists())
        self.assertIn("请等待",self.window.status.get())
        self.window.busy = False

    def test_minimum_layout_keeps_list_and_actions_visible(self):
        self.tk.deiconify()
        self.window.win.deiconify()
        self.window.win.geometry("800x560+0+0")
        self.tk.update()
        self.window.detail.set("已选 1 项\n原片依赖："+"这是一部影片的名称"*30+"\n"+str(self.root/('很长的缓存路径'*30)))
        self.window.status.set("迁移完成，旧副本保留："+"很长的说明资料与缓存路径/"*60)
        self.tk.update()
        self.assertGreater(self.window.tree.winfo_height(),60)
        self.assertLess(self.window.tree.winfo_rooty()+self.window.tree.winfo_height(),
                        self.window.win.winfo_rooty()+self.window.win.winfo_height())
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        buttons = [widget for widget in descendants(self.window.body) if widget.winfo_class()=="TButton"]
        self.assertGreaterEqual(len(buttons),7)
        body = self.window.body
        left,top = body.winfo_rootx(),body.winfo_rooty()
        right,bottom = left+body.winfo_width(),top+body.winfo_height()
        for button in buttons:
            with self.subTest(button=button.cget("text")):
                self.assertTrue(button.winfo_ismapped())
                self.assertGreater(button.winfo_width(),1)
                self.assertGreaterEqual(button.winfo_rootx(),left)
                self.assertGreaterEqual(button.winfo_rooty(),top)
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),right)
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),bottom)
        self.assertEqual(len(self.window.action_buttons),4)
        self.assertLessEqual(self.window.status_text.winfo_rooty()+self.window.status_text.winfo_height(),bottom)


if __name__=="__main__":
    unittest.main()
