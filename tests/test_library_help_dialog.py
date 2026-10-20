"""Library-only F1 and help button on a private Windows desktop."""
import ctypes as C
import unittest
from unittest.mock import patch
import test_bookmarks_dialog as harness

wx = harness.wx

@unittest.skipUnless(harness.ISOLATED and wx is not None, 'Private Windows desktop required')
class LibraryHelpTests(unittest.TestCase):
    setUpClass = classmethod(harness.BookmarkDialogTests.setUpClass.__func__)
    setUp = harness.BookmarkDialogTests.setUp
    tearDown = harness.BookmarkDialogTests.tearDown
    library = harness.BookmarkDialogTests.library
    key = harness.BookmarkDialogTests.key
    modal_check = harness.BookmarkDialogTests.modal_check

    def test_library_has_no_native_menu_bar(self):
        window = self.library()
        self.assertIsNone(window.GetMenuBar())
        user32 = C.WinDLL('user32')
        user32.GetMenu.argtypes = [C.c_void_p]
        user32.GetMenu.restype = C.c_void_p
        self.assertFalse(user32.GetMenu(window.GetHandle()))

    def test_six_buttons_only_with_help_before_close_and_native_tab_cycle(self):
        window = self.library()
        buttons = [c for c in window.panel.GetChildren() if isinstance(c, wx.Button)]
        self.assertEqual([b.GetLabel() for b in buttons], [
            'Słuchaj', 'Pauza / wznów (Ctrl+P)', 'Dodaj archiwum', 'Dodaj folder',
            'Pomoc (F1)', 'Zamknij (Escape)'])
        for playing, books in ((False, self.books), (True, self.books), (False, [])):
            self.host.is_playing.return_value = playing
            window.refresh(books)
            window.bookList.SetFocus()
            expected = [window.bookList] + [b for b in buttons if b.IsShown()]
            visited = []
            for _ in expected:
                focus = wx.Window.FindFocus()
                visited.append(focus)
                focus.Navigate()
            self.assertEqual(visited, expected)
            self.assertIs(wx.Window.FindFocus(), window.bookList)

    def test_f1_from_list_status_and_button_once_per_press(self):
        window = self.library()
        for control in (window.bookList, window.status, window.zipButton):
            control.SetFocus()
            self.host.show_help.reset_mock()
            self.assertFalse(self.key(window, wx.WXK_F1).GetSkipped())
            self.key(window, wx.WXK_F1, repeat=True)
            self.host.show_help.assert_called_once_with()
            self.host.action.assert_not_called()
        window.refresh([])
        window.zipButton.SetFocus()
        self.host.show_help.reset_mock()
        self.key(window, wx.WXK_F1)
        self.host.show_help.assert_called_once_with()

    def test_f1_with_modifiers_is_not_intercepted(self):
        window = self.library()
        for modifiers in ({'ctrl': True}, {'alt': True}, {'shift': True}):
            self.assertTrue(self.key(window, wx.WXK_F1, **modifiers).GetSkipped())
        event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
        event.SetKeyCode(wx.WXK_F1)
        event.SetMetaDown(True)
        window.on_key(event)
        self.assertTrue(event.GetSkipped())
        self.host.show_help.assert_not_called()

    def test_f1_outside_library_or_in_modal_child_is_not_intercepted(self):
        window = self.library()
        other = wx.Frame(None)
        self.windows.append(other)
        field = wx.TextCtrl(other)
        with patch.object(window, 'FindFocus', return_value=field):
            self.assertTrue(self.key(window, wx.WXK_F1).GetSkipped())
        child = wx.Dialog(window)
        self.windows.append(child)
        editor = wx.TextCtrl(child)
        def check():
            editor.SetFocus()
            self.assertTrue(self.key(window, wx.WXK_F1).GetSkipped())
            self.host.show_help.assert_not_called()
        self.modal_check(child, check)

    def test_help_button_uses_same_action_without_playback(self):
        window = self.library()
        buttons = [c for c in window.panel.GetChildren()
                   if isinstance(c, wx.Button) and c.GetLabel() == 'Pomoc (F1)']
        self.assertEqual(len(buttons), 1)
        button = buttons[0]
        event = wx.CommandEvent(wx.wxEVT_BUTTON, button.GetId())
        event.SetEventObject(button)
        button.ProcessWindowEvent(event)
        self.host.show_help.assert_called_once_with()
        self.host.action.assert_not_called()
        self.host.dialog_closed.assert_not_called()

if __name__ == '__main__':
    unittest.main()
