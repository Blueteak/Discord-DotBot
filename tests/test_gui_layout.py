"""Opt-in real Tk layout checks: DOTBOT_TEST_GUI=1, graphical desktop required."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dotbot.gui import Window
from dotbot.launcher import Launcher, FAILURE


@unittest.skipUnless(os.environ.get('DOTBOT_TEST_GUI') == '1', 'Run on graphical Linux desktop with DOTBOT_TEST_GUI=1')
class LayoutTests(unittest.TestCase):
    def test_fixed_pages_fit_without_scrolling_or_connecting(self):
        import tkinter as tk
        from tkinter import ttk
        with tempfile.TemporaryDirectory() as folder:
            spawn = Mock()
            controller = Launcher(Path(folder) / 'private', spawn=spawn)
            root = tk.Tk()
            try:
                window = Window(root, controller, tk, ttk)
                for size in ('1180x840', '980x780'):
                    root.geometry(size)
                    for page in range(4):
                        for scope in ('accessible', 'scoped'):
                            for saved in (False, True):
                                controller.saved_token = saved
                                window.replacing_token = saved
                                window.page = page
                                window.scope.set(scope)
                                window.render()
                                window.status.set(FAILURE)
                                root.update_idletasks()
                                if page == 0:
                                    labels = [str(w.cget('text')) for w in window.body.winfo_children() if isinstance(w, ttk.Label)]
                                    self.assertFalse(any(text.startswith('Handle') for text in labels))
                                    self.assertIn('Discord user ID - 15 to 20 digits', labels)
                                    self.assertIn('Use Copy User ID, not your @handle.', labels)
                                if page == 1:
                                    labels = [str(w.cget('text')) for w in window.body.winfo_children() if isinstance(w, ttk.Label)]
                                    checks = [str(w.cget('text')) for w in window.body.winfo_children() if isinstance(w, ttk.Checkbutton)]
                                    self.assertIn('Use this computer’s network proxy', checks)
                                    self.assertIn('Uses this computer’s network gateway.\nRequired in Dot’s cloud; usually off on your own computer.', labels)
                                    self.assertFalse(window.proxy.get())
                                self.assertLessEqual(window.body.winfo_reqheight(), window.body.winfo_height(), (size, page, scope))
                                for widget in window.body.winfo_children():
                                    self.assertGreaterEqual(widget.winfo_y(), 0)
                                    self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), window.body.winfo_height())
                                for widget in (window.action, window.back_button, window.status_label):
                                    self.assertTrue(widget.winfo_ismapped())
                                    bottom = widget.winfo_rooty() - root.winfo_rooty() + widget.winfo_height()
                                    self.assertLessEqual(bottom, root.winfo_height())
                                self.assertLessEqual(window.status_label.winfo_reqheight(), window.status_label.master.winfo_height())
                spawn.assert_not_called()
                self.assertFalse(controller.directory.exists())
            finally:
                root.destroy()
