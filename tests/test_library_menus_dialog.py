"""Native menus on the verified private desktop; never inject global input."""
import ctypes as C
import unittest
from unittest.mock import patch
import test_bookmarks_dialog as harness

wx = harness.wx


@unittest.skipUnless(harness.ISOLATED and wx is not None, 'Private Windows desktop required')
class LibraryMenuTests(unittest.TestCase):
    setUpClass = classmethod(harness.BookmarkDialogTests.setUpClass.__func__)
    setUp = harness.BookmarkDialogTests.setUp
    tearDown = harness.BookmarkDialogTests.tearDown
    library = harness.BookmarkDialogTests.library
    key = harness.BookmarkDialogTests.key
    modal_check = harness.BookmarkDialogTests.modal_check






    def test_context_commands_capture_mouse_or_keyboard_book_and_ignore_empty(self):
        window = self.library()
        labels = ['Słuchaj', 'Zakładki\tCtrl+B', 'Wróć do słuchania\tCtrl+Shift+B',
                  'Przesunięcie numeracji odcinków…\tCtrl+N', 'Usuń\tDelete']
        methods = ['start_book', 'show_bookmarks', 'return_from_bookmark', 'show_numbering', 'remove_book']
        for n, method in enumerate(methods):
            for mouse in (False, True):
                window.bookList.SetSelection(1)
                self.host.reset_mock()
                def popup(menu):
                    items = menu.GetMenuItems()
                    self.assertEqual([i.GetItemLabel() for i in items], labels)
                    window.bookList.SetSelection(1 if mouse else 0)
                    menu.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, items[n].GetId()))
                with patch.object(window.bookList, 'PopupMenu', side_effect=popup):
                    event = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU)
                    event.SetPosition(window.bookList.ClientToScreen(wx.Point(5, 5)) if mouse else wx.DefaultPosition)
                    window.bookList.ProcessWindowEvent(event)
                getattr(self.host, method).assert_called_once_with('a' if mouse else 'b')
                self.assertIs(wx.Window.FindFocus(), window.bookList)
        with patch.object(window.bookList, 'PopupMenu') as popup:
            event.SetPosition(window.bookList.ClientToScreen(wx.Point(5, 180)))
            window.bookList.ProcessWindowEvent(event)
            window.refresh([])
            event.SetPosition(wx.DefaultPosition)
            window.bookList.ProcessWindowEvent(event)
            popup.assert_not_called()


    def test_modal_child_keys_do_not_trigger_parent_playback_or_close(self):
        window = self.library()
        child = wx.Dialog(window)
        self.windows.append(child)
        editor = wx.TextCtrl(child)
        def check():
            editor.SetFocus()
            for key, mods in ((ord('P'), {'ctrl': True}), (ord('I'), {'ctrl': True}),
                              (wx.WXK_LEFT, {'alt': True}), (wx.WXK_ESCAPE, {}),
                              (ord('B'), {'ctrl': True}), (ord('N'), {'ctrl': True})):
                event = self.key(window, key, **mods)
                self.assertTrue(event.GetSkipped())
            self.host.action.assert_not_called()
            self.host.dialog_closed.assert_not_called()
            self.host.show_bookmarks.assert_not_called()
            self.assertIs(wx.Window.FindFocus(), editor)
        self.modal_check(child, check)



    def test_apps_hook_opens_native_context_menu_escape_restores_selected_book(self):
        self.native_menu_session('apps')

    def test_shift_f10_hook_opens_native_context_menu(self):
        self.native_menu_session('shift_f10')

    def test_native_keyboard_context_message_opens_menu_for_selection(self):
        # Windows sends this message for Shift+F10; (-1,-1) denotes keyboard.
        self.native_menu_session('keyboard_context')

    def native_menu_session(self, source):
        import threading
        import time
        from ctypes import wintypes as W
        window = self.library()
        u = C.WinDLL('user32', use_last_error=True)
        k = C.WinDLL('kernel32')
        class GuiInfo(C.Structure):
            _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD),
                        ('active', W.HWND), ('focus', W.HWND), ('capture', W.HWND),
                        ('menuOwner', W.HWND), ('moveSize', W.HWND), ('caret', W.HWND),
                        ('rect', W.RECT)]
        u.GetGUIThreadInfo.argtypes = [W.DWORD, C.POINTER(GuiInfo)]
        u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u.SendMessageW.restype = W.LPARAM
        thread_id, hwnd = k.GetCurrentThreadId(), window.GetHandle()
        u.GetThreadDesktop.argtypes = [W.DWORD]
        u.GetThreadDesktop.restype = W.HANDLE
        u.SetThreadDesktop.argtypes = [W.HANDLE]
        desktop = u.GetThreadDesktop(thread_id)
        observed = []
        window.bookList.SetSelection(1)
        def dismiss():
            u.SetThreadDesktop(desktop)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                info = GuiInfo(cbSize=C.sizeof(GuiInfo))
                if u.GetGUIThreadInfo(thread_id, C.byref(info)) and info.flags & 4:
                    observed.append((info.flags, info.menuOwner))
                    u.PostMessageW(hwnd, 0x100, 0x1b, 1)
                    u.PostMessageW(hwnd, 0x101, 0x1b, 0xc0000001)
                    return
                time.sleep(0.005)
            u.PostMessageW(hwnd, 0x1f, 0, 0)
        guard = threading.Thread(target=dismiss, daemon=True)
        guard.start()
        target = window.bookList.GetHandle()
        if source == 'apps':
            self.key(window, wx.WXK_WINDOWS_MENU)
        elif source == 'shift_f10':
            self.key(window, wx.WXK_F10, shift=True)
        else:
            u.SendMessageW(target, 0x7b, target, -1)
        wx.YieldIfNeeded()
        guard.join(3)
        wx.YieldIfNeeded()
        self.assertTrue(observed, 'Native ' + source + ' must enter Windows menu mode')
        self.assertIn(observed[0][1], (hwnd, target))
        self.assertEqual(window.selected()['id'], 'b')
        self.host.dialog_closed.assert_not_called()
        self.host.action.assert_not_called()
        self.assertIs(wx.Window.FindFocus(), window.bookList)


    def test_context_keys_ignore_repeat_wrong_focus_modifiers_and_empty_selection(self):
        window = self.library()
        for key, shift in ((wx.WXK_WINDOWS_MENU, False), (wx.WXK_F10, True)):
            with patch.object(window.bookList, 'PopupMenu') as popup:
                self.assertFalse(self.key(window, key, shift=shift).GetSkipped())
                popup.assert_called_once()
                self.key(window, key, shift=shift, repeat=True)
                self.key(window, key, shift=shift, ctrl=True)
                self.key(window, key, shift=shift, alt=True)
                window.zipButton.SetFocus()
                self.key(window, key, shift=shift)
                popup.assert_called_once()
                window.bookList.SetFocus()
        window.refresh([])
        with patch.object(window.bookList, 'PopupMenu') as popup:
            self.key(window, wx.WXK_WINDOWS_MENU)
            self.key(window, wx.WXK_F10, shift=True)
            popup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
