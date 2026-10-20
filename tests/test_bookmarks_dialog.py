"""Native wxMSW tests, opt-in PRIVATE desktop only; no NVDA or global input.

Run: python tests/test_bookmarks_dialog.py --isolated -v
Never SwitchDesktop: the user's visible desktop and speech stay untouched.
"""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ISOLATED = False
_desktop = None
if sys.platform == 'win32' and __name__ == '__main__' and '--isolated' in sys.argv:
    sys.argv.remove('--isolated')
    import ctypes as C
    u = C.WinDLL('user32', use_last_error=True)
    u.CreateDesktopW.argtypes = [C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_ulong, C.c_ulong, C.c_void_p]
    u.CreateDesktopW.restype = C.c_void_p
    u.SetThreadDesktop.argtypes = [C.c_void_p]
    _desktop = u.CreateDesktopW('CzytelniaBookmarkTests_' + str(os.getpid()), None, None, 0, 0x1ff, None)
    if not _desktop or not u.SetThreadDesktop(_desktop):
        raise C.WinError(C.get_last_error())
    ISOLATED = True

# Also accept the parent's verified, non-input test desktop runner.
if sys.platform == 'win32':
    import ctypes as C
    u = C.WinDLL('user32', use_last_error=True)
    k = C.WinDLL('kernel32')
    u.GetThreadDesktop.argtypes = [C.c_ulong]
    u.GetThreadDesktop.restype = C.c_void_p
    u.OpenInputDesktop.argtypes = [C.c_ulong, C.c_int, C.c_ulong]
    u.OpenInputDesktop.restype = C.c_void_p
    u.CloseDesktop.argtypes = [C.c_void_p]
    u.GetUserObjectInformationW.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong)]
    def desktop_name(handle):
        value = C.create_unicode_buffer(256)
        needed = C.c_ulong()
        if not u.GetUserObjectInformationW(handle, 2, value, C.sizeof(value), C.byref(needed)):
            raise C.WinError(C.get_last_error())
        return value.value
    current_name = desktop_name(u.GetThreadDesktop(k.GetCurrentThreadId()))
    input_desktop = u.OpenInputDesktop(0, False, 1)
    if not input_desktop:
        raise C.WinError(C.get_last_error())
    try:
        ISOLATED = (current_name.startswith('CzytelniaBookmarkTests_')
                    and current_name != desktop_name(input_desktop))
    finally:
        u.CloseDesktop(input_desktop)

try:
    import wx
except ImportError:
    wx = None


@unittest.skipUnless(ISOLATED and wx is not None, 'Opt-in private Windows desktop required')
class BookmarkDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App(False)
        spec = importlib.util.spec_from_file_location('bookmark_dialogs_native',
            Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/dialogs.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.host = Mock()
        self.host.is_playing.return_value = False
        self.books = [{'id': 'a', 'title': 'Pierwsza'}, {'id': 'b', 'title': 'Druga'}]
        self.windows = []

    def tearDown(self):
        for window in reversed(self.windows):
            if window and not window.IsBeingDeleted():
                window.Destroy()
        self.app.ProcessPendingEvents()

    def library(self):
        window = self.module.LibraryDialog(None, self.host, self.books)
        self.windows.append(window)
        window.Show()
        window.bookList.SetFocus()
        self.app.ProcessPendingEvents()
        return window

    def key(self, target, key, *, ctrl=False, shift=False, alt=False, repeat=False):
        event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
        event.SetKeyCode(key)
        event.SetControlDown(ctrl)
        event.SetShiftDown(shift)
        event.SetAltDown(alt)
        event.IsAutoRepeat = lambda: repeat
        target.on_key(event)
        return event

    def test_library_ctrl_d_once_per_keypress_ignores_selection(self):
        window = self.library()
        window.bookList.SetSelection(1)
        self.key(window, ord('D'), ctrl=True)
        self.key(window, ord('D'), ctrl=True, repeat=True)
        self.host.add_bookmark.assert_called_once_with()
        self.host.start_book.assert_not_called()

    def test_library_ctrl_b_passes_only_selected_book(self):
        window = self.library()
        window.bookList.SetSelection(1)
        self.key(window, ord('B'), ctrl=True)
        self.host.show_bookmarks.assert_called_once_with('b')
        self.host.start_book.assert_not_called()
    def bookmarks(self):
        self.assertTrue(hasattr(self.module, 'BookmarksDialog'), 'Missing native bookmark list')
        rows = [{'id': 'x', 'name': 'Zakładka1', 'track_index': 0, 'position': 61.9},
                {'id': 'y', 'name': 'Mój fragment', 'track_index': 2, 'position': 3723.2}]
        window = self.module.BookmarksDialog(None, self.host, self.books[1], rows)
        self.windows.append(window)
        return window

    def modal_check(self, window, check, *, auto_close=True):
        errors = []
        def run():
            try:
                check()
            except BaseException as error:
                errors.append(error)
            finally:
                if window.IsModal() and (auto_close or errors):
                    window.EndModal(wx.ID_CANCEL)
        timeout = wx.CallLater(3000, lambda: window.IsModal() and window.EndModal(wx.ID_CANCEL))
        wx.CallAfter(run)
        result = window.ShowModal()
        timeout.Stop()
        if errors:
            raise errors[0]
        return result

    def test_list_labels_seconds_browsing_and_enter(self):
        window = self.bookmarks()
        self.assertEqual(window.bookmarkList.GetStrings(), [
            'Zakładka1 — odcinek 1 — 0:01:01', 'Mój fragment — odcinek 3 — 1:02:03'])
        def check():
            window.bookmarkList.SetSelection(1)
            window.bookmarkList.SetFocus()
            self.key(window, wx.WXK_UP)
            self.host.start_bookmark.assert_not_called()
            self.key(window, wx.WXK_RETURN)
            self.host.start_bookmark.assert_called_once_with(window, 'y')
        self.modal_check(window, check)

    def test_tab_order_and_escape(self):
        window = self.bookmarks()
        def check():
            window.bookmarkList.SetFocus()
            actual = []
            for _ in range(4):
                focus = wx.Window.FindFocus()
                actual.append(focus)
                focus.Navigate()
            self.assertEqual(actual, [window.bookmarkList, window.renameButton,
                                      window.removeButton, window.closeButton])
            self.assertEqual(window.renameButton.GetLabel(), 'Zmień nazwę (F2)')
            self.key(window, wx.WXK_ESCAPE)
            self.assertTrue(window._closed)
        self.assertEqual(self.modal_check(window, check), wx.ID_CANCEL)
    def button(self, button):
        event = wx.CommandEvent(wx.wxEVT_BUTTON, button.GetId())
        button.GetEventHandler().ProcessEvent(event)

    def test_f2_button_and_context_menu_share_selected_rename(self):
        window = self.bookmarks()
        def check():
            window.bookmarkList.SetSelection(1)
            window.bookmarkList.SetFocus()
            self.key(window, wx.WXK_F2)
            self.button(window.renameButton)
            self.assertEqual(self.host.rename_bookmark.call_args_list,
                             [((window, 'y'),), ((window, 'y'),)])
            def popup(menu):
                items = menu.GetMenuItems()
                self.assertEqual([i.GetItemLabel() for i in items], ['Zmień nazwę\tF2', 'Usuń'])
                event = wx.CommandEvent(wx.wxEVT_MENU, items[0].GetId())
                menu.ProcessEvent(event)
            with patch.object(window.bookmarkList, 'PopupMenu', side_effect=popup):
                event = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU)
                event.SetPosition(wx.DefaultPosition)
                window.bookmarkList.GetEventHandler().ProcessEvent(event)
            self.host.rename_bookmark.assert_called_with(window, 'y')
            self.assertEqual(self.host.rename_bookmark.call_count, 3)
        self.modal_check(window, check)

    def test_mouse_context_targets_clicked_item_and_blank_area_does_nothing(self):
        window = self.bookmarks()
        def check():
            window.bookmarkList.SetSelection(1)
            observed = []
            def popup(menu):
                observed.append(window.selected()['id'])
                menu.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, menu.GetMenuItems()[1].GetId()))
            with patch.object(window.bookmarkList, 'PopupMenu', side_effect=popup):
                event = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU)
                event.SetPosition(window.bookmarkList.ClientToScreen(wx.Point(5, 5)))
                window.bookmarkList.GetEventHandler().ProcessEvent(event)
                self.assertEqual(observed, ['x'])
                self.host.remove_bookmark.assert_called_once_with(window, 'x')
                event.SetPosition(window.bookmarkList.ClientToScreen(wx.Point(5, 180)))
                window.bookmarkList.GetEventHandler().ProcessEvent(event)
                self.assertEqual(observed, ['x'])
        self.modal_check(window, check)

    def test_rename_dialog_presets_selects_name_validates_and_cancels(self):
        self.assertTrue(hasattr(self.module, 'BookmarkNameDialog'), 'Missing accessible rename dialog')
        window = self.module.BookmarkNameDialog(None, 'Stara nazwa')
        self.windows.append(window)
        self.assertEqual(window.name.GetValue(), 'Stara nazwa')
        self.assertEqual(window.name.GetSelection(), (0, len('Stara nazwa')))
        self.assertEqual(window.name.GetName(), 'Nazwa zakładki')
        labels = [c.GetLabel() for c in window.GetChildren() if isinstance(c, wx.StaticText)]
        self.assertIn('Nazwa zakładki:', labels)
        window.name.SetValue('   ')
        with patch.object(wx, 'MessageBox') as message, patch.object(window, 'EndModal') as end:
            self.button(window.FindWindow(wx.ID_OK))
            message.assert_called_once()
            end.assert_not_called()
        window.name.SetValue('  Nowa nazwa  ')
        self.assertEqual(window.bookmark_name(), 'Nowa nazwa')
        self.assertEqual(self.modal_check(window, lambda: self.button(window.FindWindow(wx.ID_CANCEL)), auto_close=False), wx.ID_CANCEL)
        # A real native modal OK result, not mocked EndModal.
        self.assertEqual(self.modal_check(window, lambda: self.button(window.FindWindow(wx.ID_OK)), auto_close=False), wx.ID_OK)
    def test_focus_restore_tracks_id_after_reorder_and_delete_keeps_neighbor(self):
        window = self.bookmarks()
        self.assertTrue(hasattr(window, 'focus_bookmark'), 'Missing bookmark focus restoration')
        def check():
            window.bookmarkList.SetSelection(1)
            window.refresh(list(reversed(window.bookmarks)), selected_id='y')
            self.assertEqual(window.selected()['id'], 'y')
            window.focus_bookmark('x')
            self.assertEqual(window.selected()['id'], 'x')
            self.assertIs(wx.Window.FindFocus(), window.bookmarkList)
            window.refresh([b for b in window.bookmarks if b['id'] != 'x'], selected_id='x')
            self.assertEqual(window.selected()['id'], 'y')
        self.modal_check(window, check)
    def test_native_msaa_names_roles_and_list_entries(self):
        import uuid
        class Value(C.Union):
            _fields_ = [('number', C.c_long), ('storage', C.c_byte * (16 if C.sizeof(C.c_void_p) == 8 else 8))]
        class Variant(C.Structure):
            _fields_ = [('vt', C.c_ushort), ('reserved', C.c_ushort * 3), ('value', Value)]
        class GUID(C.Structure):
            _fields_ = [('data', C.c_byte * 16)]
        iid = GUID.from_buffer_copy(uuid.UUID('618736e0-3c3d-11cf-810c-00aa00389b71').bytes_le)
        oleacc, oleaut = C.WinDLL('oleacc'), C.WinDLL('oleaut32')
        oleacc.AccessibleObjectFromWindow.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(GUID), C.POINTER(C.c_void_p)]
        oleacc.AccessibleObjectFromWindow.restype = C.c_long
        oleaut.SysFreeString.argtypes = [C.c_void_p]
        def accessible(control, child_id=0):
            pointer = C.c_void_p()
            self.assertEqual(oleacc.AccessibleObjectFromWindow(control.GetHandle(), 0xFFFFFFFC,
                              C.byref(iid), C.byref(pointer)), 0)
            vtable = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
            child = Variant()
            child.vt, child.value.number = 3, child_id
            text, role = C.c_void_p(), Variant()
            try:
                name_fn = C.WINFUNCTYPE(C.c_long, C.c_void_p, Variant, C.POINTER(C.c_void_p))(vtable[10])
                role_fn = C.WINFUNCTYPE(C.c_long, C.c_void_p, Variant, C.POINTER(Variant))(vtable[13])
                self.assertEqual(name_fn(pointer, child, C.byref(text)), 0)
                self.assertEqual(role_fn(pointer, child, C.byref(role)), 0)
                return C.wstring_at(text), role.value.number
            finally:
                if text.value:
                    oleaut.SysFreeString(text)
                C.WINFUNCTYPE(C.c_ulong, C.c_void_p)(vtable[2])(pointer)
        window = self.bookmarks()
        def check_list():
            name, role = accessible(window.bookmarkList)
            self.assertIn('Zakładki', name)
            self.assertEqual(role, 33)  # ROLE_SYSTEM_LIST
            self.assertEqual(accessible(window.bookmarkList, 2),
                             ('Mój fragment — odcinek 3 — 1:02:03', 34))
        self.modal_check(window, check_list)
        rename = self.module.BookmarkNameDialog(None, 'Stara nazwa')
        self.windows.append(rename)
        def check_name():
            name, role = accessible(rename.name)
            self.assertIn('Nazwa zakładki', name)
            self.assertEqual(role, 42)  # ROLE_SYSTEM_TEXT
        self.modal_check(rename, check_name)
    def test_native_nested_modals_keep_core_pump_alive_and_restore_library_focus(self):
        self._native_workflow()

    def test_native_terminate_during_nested_modal_is_safe(self):
        self._native_workflow(terminate=True)

    def _native_workflow(self, *, terminate=False):
        import test_plugin as harness
        fixture = harness.PluginTests('test_only_requested_global_gesture')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        plugin = fixture.plugin
        fixture.module.wx = wx
        sys.modules['wx'] = wx  # wx internals import their own public module.
        fixture.modules['gui'].mainFrame.prePopup = Mock()
        fixture.modules['gui'].mainFrame.postPopup = Mock()
        fixture.modules['plugin_test.dialogs'].BookmarksDialog = self.module.BookmarksDialog
        fixture.modules['plugin_test.dialogs'].BookmarkNameDialog = self.module.BookmarkNameDialog
        controller = plugin._controller = Mock()
        controller.player.is_playing = False
        rows = [{'id': 'one', 'name': 'Zakładka1', 'track_index': 0, 'position': 61.5},
                {'id': 'two', 'name': 'Zakładka2', 'track_index': 1, 'position': 72.5}]
        controller.library.get_book.return_value = self.books[1]
        controller.library.bookmarks.side_effect = lambda key: [b.copy() for b in rows]
        controller.library.get_bookmark.side_effect = lambda book, key: next(b.copy() for b in rows if b['id'] == key)
        def rename(book, key, name):
            next(b for b in rows if b['id'] == key)['name'] = name
        controller.library.rename_bookmark.side_effect = rename
        parent = plugin._dialog = self.module.LibraryDialog(None, plugin, self.books)
        self.windows.append(parent)
        parent.Show()
        parent.bookList.SetSelection(1)
        parent.bookList.SetFocus()
        state = {'core': False, 'ticks': 0, 'finished': False, 'modals': 0}
        errors, timers = [], []
        def later(delay, fn):
            def guarded():
                if state['finished']:
                    return
                try:
                    fn()
                except BaseException as error:
                    import traceback
                    traceback.print_exc()
                    errors.append(error)
                    finish()
            timers.append(wx.CallLater(delay, guarded))
        def finish():
            state['finished'] = True
            plugin._close_bookmarks()
            self.app.ExitMainLoop()
        def heartbeat():
            state['core'] = True
            state['ticks'] += 1
            if state['ticks'] == 1:
                plugin.show_bookmarks('b')
            state['core'] = False
            later(10, heartbeat)
        original_modal = plugin._modal
        def observe(dialog):
            self.assertFalse(state['core'], 'Modal entered inside simulated NVDA CorePump')
            initial_ticks = state['ticks']
            state['modals'] += 1
            if isinstance(dialog, self.module.BookmarksDialog):
                dialog.bookmarkList.SetSelection(1)
                later(30, lambda: dialog.on_rename(None))
                def done():
                    if dialog.selected()['name'] != 'Zmieniona':
                        later(10, done)
                        return
                    self.assertEqual(dialog.selected()['id'], 'two')
                    self.assertIs(wx.Window.FindFocus(), dialog.bookmarkList)
                    self.key(dialog, wx.WXK_ESCAPE)
                    def returned():
                        self.assertEqual(parent.selected()['id'], 'b')
                        self.assertIs(wx.Window.FindFocus(), parent.bookList)
                        finish()
                    later(30, returned)
                if not terminate:
                    later(110, done)
            else:
                self.assertIsInstance(dialog, self.module.BookmarkNameDialog)
                self.assertEqual(dialog.name.GetValue(), 'Zakładka2')
                def accept():
                    if terminate:
                        plugin.terminate()
                        later(30, finish)
                    else:
                        dialog.name.SetValue('Zmieniona')
                        self.button(dialog.FindWindow(wx.ID_OK))
                later(35, accept)
            answer = original_modal(dialog)
            self.assertGreater(state['ticks'], initial_ticks, 'NVDA heartbeat stopped inside modal')
            return answer
        plugin._modal = observe
        later(1, heartbeat)
        def timeout():
            errors.append(AssertionError('Bookmark modal workflow timed out'))
            finish()
        later(3000, timeout)
        try:
            self.app.MainLoop()
        finally:
            for timer in timers:
                timer.Stop()
            plugin.terminate()
        if errors:
            raise errors[0]
        self.assertEqual(state['modals'], 2)
        if terminate:
            controller.library.rename_bookmark.assert_not_called()
        else:
            controller.library.rename_bookmark.assert_called_once_with('b', 'two', 'Zmieniona')
        controller.start_bookmark.assert_not_called()
        controller.player.pause.assert_not_called()
    def test_shortcuts_do_not_escape_nested_rename_editor(self):
        library = self.library()
        window = self.bookmarks()
        # Real focused editor on another top-level dialog: parent shortcuts
        # must not run even if a CHAR_HOOK propagates from that child.
        rename = self.module.BookmarkNameDialog(window, 'Zakładka1')
        self.windows.append(rename)
        def check():
            rename.name.SetFocus()
            self.key(library, ord('D'), ctrl=True)
            self.key(library, ord('B'), ctrl=True)
            self.key(window, wx.WXK_F2)
            self.host.add_bookmark.assert_not_called()
            self.host.show_bookmarks.assert_not_called()
            self.host.rename_bookmark.assert_not_called()
        self.modal_check(rename, check)


if __name__ == '__main__':
    def timed_out():
        import faulthandler
        faulthandler.dump_traceback()
        os._exit(124)
    watchdog = threading.Timer(15, timed_out)
    watchdog.daemon = True
    watchdog.start()
    try:
        unittest.main()
    finally:
        watchdog.cancel()
