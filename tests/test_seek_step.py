"""UI seek contract without opening a real window or loading NVDA."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class SeekStepTests(unittest.TestCase):
    def test_control_arrows_request_five_seconds_in_both_directions(self):
        wx = types.SimpleNamespace(Dialog=object, Frame=object, WXK_LEFT=314, WXK_RIGHT=316,
                                   WXK_SPACE=32, WXK_RETURN=13, WXK_ESCAPE=27,
                                   WXK_WINDOWS_MENU=395, WXK_F10=349, WXK_F1=340)
        spec = importlib.util.spec_from_file_location('seek_step_dialog',
            ROOT / 'addon/globalPlugins/czytelnia/dialogs.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': wx}):
            spec.loader.exec_module(module)
        for key, delta in ((wx.WXK_LEFT, -5), (wx.WXK_RIGHT, 5)):
            with self.subTest(delta=delta):
                dialog = Mock(_adjustment=None)
                dialog.bookList = object()
                dialog.FindFocus.return_value = dialog.bookList
                wx.GetTopLevelParent = lambda focus: dialog
                event = Mock()
                event.GetKeyCode.return_value = key
                event.ControlDown.return_value = True
                event.AltDown.return_value = False
                event.ShiftDown.return_value = False
                event.MetaDown.return_value = False
                module.LibraryDialog.on_key(dialog, event)
                dialog._start_adjustment.assert_called_once_with(event, 'seek_relative', delta)
                event.Skip.assert_not_called()

    def test_build_has_supported_version_and_minimum_nvda(self):
        spec = importlib.util.spec_from_file_location('seek_step_build', ROOT / 'build.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fields = module.validate_manifest((ROOT / 'addon/manifest.ini').read_text(encoding='utf-8'))
        self.assertGreaterEqual(tuple(map(int, fields['version'].split('.'))), (0, 1, 25))
        self.assertEqual(fields['minimumNVDAVersion'], '2025.1')



if __name__ == '__main__':
    unittest.main()
