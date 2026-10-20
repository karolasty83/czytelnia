"""Production wx key dispatch plus real host/controller, isolated from NVDA."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

if os.name == 'nt':
    import wx

import test_adjustment_plugin


@unittest.skipUnless(os.name == 'nt', 'Real Windows wx required')
class AdjustmentDialogRuntimeTests(unittest.TestCase):
    def test_pause_key_cannot_resume_between_dialog_release_and_action(self):
        app = wx.GetApp() or wx.App(False)
        fixture = test_adjustment_plugin.AdjustmentPluginTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plugin, controller = fixture.p, fixture.core.c
        with patch.dict(sys.modules, {'wx': wx}):
            path = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/dialogs.py'
            spec = importlib.util.spec_from_file_location('adjustment_dialog_runtime', path)
            dialogs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(dialogs)
            for key, ctrl in ((ord('P'), True), (wx.WXK_SPACE, False)):
                with self.subTest(key=key):
                    controller.player.is_playing = True
                    dialog = dialogs.LibraryDialog(None, plugin, [{'id': 'abc', 'title': 'Test'}])
                    plugin._dialog = dialog
                    try:
                        plugin.adjust('seek_relative', 15)
                        fixture.jobs.pop(0)()
                        dialog._adjustment = {'key': wx.WXK_RIGHT,
                            'modifiers': wx.MOD_CONTROL, 'focus': dialog.bookList}
                        played = controller.player.play = Mock(wraps=controller.player.play)
                        original_end = plugin.end_adjustment
                        def worker_wins_release_race():
                            original_end()
                            while fixture.jobs:
                                fixture.jobs.pop(0)()
                        event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
                        event.SetKeyCode(key)
                        event.SetControlDown(ctrl)
                        with patch.object(plugin, 'end_adjustment', side_effect=worker_wins_release_race), \
                             patch.object(dialog, 'FindFocus', return_value=dialog.bookList):
                            dialog.on_key(event)
                        while fixture.jobs:
                            fixture.jobs.pop(0)()
                        played.assert_not_called()
                        self.assertFalse(controller.player.is_playing)
                        self.assertIsNone(dialog._adjustment)
                    finally:
                        dialog.Destroy()
            wx.Yield()


if __name__ == '__main__':
    unittest.main()
