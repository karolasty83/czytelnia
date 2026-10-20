"""Focused selection-key regression checks; no NVDA or real GUI/audio."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
import test_adjustment_plugin

ROOT = Path(__file__).resolve().parents[1]


class SelectedPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_adjustment_plugin.AdjustmentPluginTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.p, self.c = self.fixture.p, self.fixture.core.c
        self.p._dialog = Mock()
        self.p._dialog.selected.return_value = {'id': 'abc'}

    def drain(self):
        while self.fixture.jobs:
            self.fixture.jobs.pop(0)()

    def test_selected_current_pauses_then_resumes_without_reloading(self):
        load = self.c.player.load = Mock(wraps=self.c.player.load)
        self.p.action('toggle_selected')
        self.drain()
        self.assertFalse(self.c.player.is_playing)
        self.p.action('toggle_selected')
        self.drain()
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.c.player.position, 100)
        load.assert_not_called()

    def test_different_selection_is_captured_before_worker_and_global_toggle_stays_current(self):
        self.p._dialog.selected.return_value = {'id': 'other'}
        start = self.c.start = Mock()
        self.p.action('toggle_selected')
        self.p._dialog.selected.return_value = {'id': 'changed'}
        self.drain()
        start.assert_called_once_with('other')
        start.reset_mock()
        self.p.action('toggle')
        self.drain()
        start.assert_not_called()
        self.assertEqual(self.c.book_id, 'abc')
        self.assertFalse(self.c.player.is_playing)

    def test_selected_current_during_hold_pauses_without_resuming(self):
        self.p.adjust('seek_relative', 5)
        self.drain()
        play = self.c.player.play = Mock(wraps=self.c.player.play)
        self.p.action('toggle_selected')
        self.drain()
        play.assert_not_called()
        self.assertFalse(self.c.player.is_playing)

    def test_empty_selection_does_not_toggle_another_book(self):
        self.p._dialog.selected.return_value = None
        self.p.action('toggle_selected')
        self.drain()
        self.assertTrue(self.c.player.is_playing)

    def test_key_dispatch_enter_space_and_ctrl_p(self):
        wx = types.SimpleNamespace(Dialog=object, Frame=object, WXK_LEFT=314, WXK_RIGHT=316,
                                   WXK_SPACE=32, WXK_RETURN=13, WXK_ESCAPE=27,
                                   WXK_WINDOWS_MENU=395, WXK_F10=349, WXK_F1=340)
        spec = importlib.util.spec_from_file_location('selection_key_dialog',
            ROOT / 'addon/globalPlugins/czytelnia/dialogs.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': wx}):
            spec.loader.exec_module(module)
        for key, ctrl, action in ((13, False, 'toggle_selected'),
                                  (32, False, 'toggle_selected'),
                                  (ord('P'), True, 'toggle_selected')):
            with self.subTest(key=key):
                dialog = Mock(_adjustment=None)
                dialog.bookList = object()
                dialog.FindFocus.return_value = dialog.bookList
                wx.GetTopLevelParent = lambda focus: dialog
                event = Mock()
                event.GetKeyCode.return_value = key
                event.ControlDown.return_value = ctrl
                event.AltDown.return_value = False
                event.ShiftDown.return_value = False
                event.MetaDown.return_value = False
                event.IsAutoRepeat.return_value = False
                module.LibraryDialog.on_key(dialog, event)
                dialog.host.action.assert_called_once_with(action)
                dialog.stop_adjustment.assert_called_once_with(finish=False)
                event.Skip.assert_not_called()


if __name__ == '__main__':
    unittest.main()
