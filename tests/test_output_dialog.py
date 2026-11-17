"""Real native Windows selection dialog on a private test desktop."""
import sys
import unittest
from unittest.mock import Mock
try:
    import wx
except ImportError:
    wx = None
import test_plugin


@unittest.skipUnless(wx is not None and sys.platform == 'win32', 'Native wx requires Windows')
class OutputDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.fixture = test_plugin.PluginTests('test_single_press_waits_then_opens_library')
        self.fixture.setUp()
        self.p = self.fixture.plugin
        self.fixture.module.wx = wx
        self.fixture.modules['gui'].mainFrame.prePopup = Mock()
        self.fixture.modules['gui'].mainFrame.postPopup = Mock()
        # wx.Window.__bool__ imports wx dynamically; keep the real module too.
        sys.modules['wx'] = wx
        self.parent = wx.Frame(None, title='Isolated library')
        self.parent.bookList = wx.ListBox(self.parent, choices=['Test'])
        self.p._dialog = self.parent
        self.p._run = Mock()
        self.p._say = Mock()
        self.parent.Show()

    def tearDown(self):
        self.fixture.tearDown()
        self.app.ProcessPendingEvents()
        wx.YieldIfNeeded()

    def open_dialog(self, answer, selection):
        token = self.p._output_request = object()
        observed = []
        def finish():
            dialog = self.p._output_dialog
            try:
                children = list(dialog.GetChildren())
                lists = []
                while children:
                    child = children.pop()
                    if isinstance(child, wx.ListBox):
                        lists.append(child)
                    children.extend(child.GetChildren())
                observed.append((dialog.GetTitle(), list(lists[0].GetStrings()), dialog.GetSelection()))
                dialog.SetSelection(selection)
            finally:
                dialog.EndModal(answer)
        wx.CallLater(100, finish)
        self.p._open_output_devices(token, (['Głośniki USB', 'Słuchawki'], 'Słuchawki'))
        return observed

    def test_default_first_and_current_selected_native_confirmation(self):
        observed = self.open_dialog(wx.ID_OK, 0)
        self.assertEqual(observed, [('Wyjście dźwięku',
                          ['Domyślne urządzenie systemowe', 'Głośniki USB', 'Słuchawki'], 2)])
        apply = self.p._run.call_args.args[0]
        self.p._set_output_device = Mock()
        controller = Mock()
        apply(controller)
        self.p._set_output_device.assert_called_once_with(controller, '')
        self.assertIsNone(self.p._output_dialog)

    def test_native_cancel_makes_no_playback_or_persistence_request(self):
        self.open_dialog(wx.ID_CANCEL, 1)
        self.p._run.assert_not_called()
        self.assertIsNone(self.p._output_dialog)

    def test_termination_cancels_native_modal_without_late_apply(self):
        token = self.p._output_request = object()
        wx.CallLater(100, self.p.terminate)
        self.p._open_output_devices(token, (['USB'], ''))
        self.assertIsNone(self.p._output_dialog)
        self.assertIsNone(self.p._dialog)
        self.p._run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
