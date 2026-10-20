"""Real wx tests on opt-in private desktop; no global input or live NVDA."""
import ctypes as C
import importlib.util
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ISOLATED = False
if sys.platform == 'win32' and __name__ == '__main__' and '--isolated' in sys.argv:
    sys.argv.remove('--isolated')
    user32 = C.WinDLL('user32', use_last_error=True)
    user32.CreateDesktopW.argtypes = [C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_ulong, C.c_ulong, C.c_void_p]
    user32.CreateDesktopW.restype = C.c_void_p
    user32.SetThreadDesktop.argtypes = [C.c_void_p]
    desktop = user32.CreateDesktopW('CzytelniaBookmarkTests_Numbering_' + str(os.getpid()), None, None, 0, 0x1ff, None)
    if not desktop or not user32.SetThreadDesktop(desktop):
        raise C.WinError(C.get_last_error())
    ISOLATED = True

import test_bookmarks_dialog as harness
ISOLATED = ISOLATED or harness.ISOLATED
wx = harness.wx
ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(ISOLATED and wx is not None, 'Opt-in private Windows desktop required')
class NumberingUITests(unittest.TestCase):
    setUpClass = classmethod(harness.BookmarkDialogTests.setUpClass.__func__)
    setUp = harness.BookmarkDialogTests.setUp
    tearDown = harness.BookmarkDialogTests.tearDown
    library = harness.BookmarkDialogTests.library
    key = harness.BookmarkDialogTests.key
    button = harness.BookmarkDialogTests.button
    modal_check = harness.BookmarkDialogTests.modal_check

    def test_ctrl_n_targets_selection_once_and_never_steals_child_focus(self):
        window = self.library()
        window.bookList.SetSelection(1)
        self.key(window, ord('N'), ctrl=True)
        self.key(window, ord('N'), ctrl=True, repeat=True)
        self.key(window, ord('N'), ctrl=True, shift=True)
        self.key(window, ord('N'), ctrl=True, alt=True)
        self.host.show_numbering.assert_called_once_with('b')
        child = wx.Dialog(window)
        self.windows.append(child)
        editor = wx.TextCtrl(child)
        def check():
            editor.SetFocus()
            self.key(window, ord('N'), ctrl=True)
            self.host.show_numbering.assert_called_once_with('b')
        self.modal_check(child, check)
        self.host.start_book.assert_not_called()
        self.host.action.assert_not_called()

    def test_context_menu_hit_test_keyboard_and_empty(self):
        window = self.library()
        window.bookList.SetSelection(1)
        def popup(menu):
            items = menu.GetMenuItems()
            item = next(i for i in items if i.GetItemLabel() == 'Przesunięcie numeracji odcinków…\tCtrl+N')
            menu.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, item.GetId()))
        with patch.object(window.bookList, 'PopupMenu', side_effect=popup) as shown:
            event = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU)
            event.SetPosition(wx.DefaultPosition)
            window.bookList.GetEventHandler().ProcessEvent(event)
            self.host.show_numbering.assert_called_once_with('b')
            event.SetPosition(window.bookList.ClientToScreen(wx.Point(5, 5)))
            window.bookList.GetEventHandler().ProcessEvent(event)
            self.host.show_numbering.assert_called_with('a')
            event.SetPosition(window.bookList.ClientToScreen(wx.Point(5, 180)))
            window.bookList.GetEventHandler().ProcessEvent(event)
            self.assertEqual(shown.call_count, 2)
            window.refresh([])
            event.SetPosition(wx.DefaultPosition)
            window.bookList.GetEventHandler().ProcessEvent(event)
            self.assertEqual(shown.call_count, 2)
            self.key(window, ord('N'), ctrl=True)
            self.assertEqual(self.host.show_numbering.call_count, 2)

    def test_dialog_snapshot_labels_actions_and_native_cancel_ok(self):
        self.assertTrue(hasattr(self.module, 'NumberingDialog'), 'Missing numbering dialog')
        book = {'id': 'b', 'title': 'Druga', 'numbering_start': 1,
                'tracks': [{'path': 'intro.mp3'}, {'path': 'one.mp3'}, {'path': 'two.mp3'}]}
        dialog = self.module.NumberingDialog(None, book, 2)
        self.windows.append(dialog)
        self.assertEqual(dialog.GetTitle(), 'Przesunięcie numeracji odcinków')
        self.assertEqual(dialog.action.GetStrings(), ['Ustaw bieżący odcinek jako pierwszy', 'Przywróć domyślną numerację'])
        text = dialog.context.GetValue()
        for value in ('Druga', 'Plik 3 z 3', 'two.mp3', 'Odcinek 2', 'Odcinek 1'):
            self.assertIn(value, text)
        self.assertIn('Plik 3', dialog.action.GetName())
        self.assertEqual(dialog.numbering_start(), 2)
        book['tracks'].clear()
        self.assertEqual(dialog.numbering_start(), 2)
        dialog.action.SetSelection(1)
        self.assertEqual(dialog.numbering_start(), 0)
        self.assertEqual(self.modal_check(dialog, lambda: self.button(dialog.FindWindow(wx.ID_CANCEL)), auto_close=False), wx.ID_CANCEL)
        self.assertEqual(self.modal_check(dialog, lambda: self.button(dialog.FindWindow(wx.ID_OK)), auto_close=False), wx.ID_OK)

    def test_plugin_native_commit_freezes_current_track_without_playback_side_effects(self):
        self.native_workflow()

    def test_plugin_selected_unloaded_book_uses_saved_index(self):
        self.native_workflow(loaded='a')

    def test_plugin_parent_close_unwinds_before_destroying_parent(self):
        self.native_workflow(mode='close')

    def test_plugin_terminate_unwinds_modal_and_invalidates_request(self):
        self.native_workflow(mode='terminate')

    def test_plugin_cancel_does_not_write(self):
        self.native_workflow(mode='cancel')

    def test_plugin_restore_writes_zero_without_playback_changes(self):
        self.native_workflow(mode='restore')

    def test_plugin_selection_changed_before_snapshot_delivery_aborts(self):
        self.native_workflow(mode='selection')

    def test_plugin_parent_closed_before_snapshot_delivery_aborts(self):
        self.native_workflow(mode='pending_close')

    def test_plugin_read_error_releases_gate(self):
        self.native_workflow(mode='read_error')

    def test_plugin_write_error_releases_gate_and_keeps_focus(self):
        self.native_workflow(mode='write_error')

    def native_workflow(self, mode='commit', loaded='b'):
        import copy
        import test_plugin as plugin_harness
        fixture = plugin_harness.PluginTests('test_only_requested_global_gesture')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.addCleanup(fixture.doCleanups)
        plugin = fixture.plugin
        self.assertTrue(hasattr(plugin, 'show_numbering'), 'Missing numbering plugin workflow')
        fixture.module.wx = wx
        sys.modules['wx'] = wx
        fixture.modules['gui'].mainFrame.prePopup = Mock()
        fixture.modules['gui'].mainFrame.postPopup = Mock()
        fixture.modules['plugin_test.dialogs'].NumberingDialog = self.module.NumberingDialog
        controller = plugin._controller = Mock()
        controller.player.is_playing = False
        controller.book_id, controller.track_index = loaded, 2
        book = {'id': 'b', 'title': 'Druga', 'track_index': 1, 'numbering_start': 0,
                'tracks': [{'path': 'zero.mp3'}, {'path': 'one.mp3'}, {'path': 'two.mp3'}, {'path': 'three.mp3'}]}
        no_modal = mode in ('selection', 'pending_close', 'read_error')
        state = {'modals': 0, 'ticks': 0, 'core': False, 'finished': False, 'returned': False}
        main_thread = threading.get_ident()
        def read(book_id):
            self.assertNotEqual(threading.get_ident(), main_thread, 'Library read on wx thread')
            self.assertEqual(book_id, 'b')
            if mode == 'read_error':
                raise OSError('test read failure')
            return copy.deepcopy(book)
        def write(book_id, index):
            self.assertNotEqual(threading.get_ident(), main_thread, 'Library write on wx thread')
            self.assertEqual(book_id, 'b')
            if mode == 'write_error':
                raise OSError('test write failure')
            book['numbering_start'] = index
            return copy.deepcopy(book)
        controller.library.get_book.side_effect = read
        controller.library.set_numbering_start.side_effect = write
        controller.library.books.side_effect = lambda: [self.books[0], copy.deepcopy(book)]
        parent = plugin._dialog = self.module.LibraryDialog(None, plugin, self.books)
        self.windows.append(parent)
        parent.Show()
        parent.bookList.SetSelection(1)
        parent.bookList.SetFocus()
        timers, errors = [], []
        def finish():
            state['finished'] = True
            plugin._close_numbering()
            self.app.ExitMainLoop()
        def later(delay, fn):
            def guarded():
                if state['finished']:
                    return
                try:
                    fn()
                except BaseException as error:
                    errors.append(error)
                    finish()
            timers.append(wx.CallLater(delay, guarded))
        def heartbeat():
            state['core'] = True
            state['ticks'] += 1
            if state['ticks'] == 1:
                plugin.show_numbering('b')
                plugin.show_numbering('b')
                if mode == 'selection':
                    parent.bookList.SetSelection(0)
                elif mode == 'pending_close':
                    plugin.dialog_closed(parent)
                if no_modal:
                    state['returned'] = True
            state['core'] = False
            later(10, heartbeat)
        original_modal = plugin._modal
        def observe(dialog):
            self.assertFalse(state['core'])
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertIsInstance(dialog, self.module.NumberingDialog)
            state['modals'] += 1
            ticks = state['ticks']
            self.assertEqual(dialog.track_index, 2 if loaded == 'b' else 1)
            def accept():
                controller.track_index = 3  # auto-next while the modal is open
                parent.bookList.SetSelection(0)
                plugin.show_numbering('a')  # dedup including open modal
                if mode == 'close':
                    plugin.dialog_closed(parent)
                    self.assertIsNone(plugin._numbering_request, 'Closing parent must invalidate pending numbering')
                    self.assertTrue(parent and not parent.IsBeingDeleted(), 'Parent destroyed inside child modal')
                elif mode == 'terminate':
                    plugin.terminate()
                    self.assertIsNone(plugin._numbering_request, 'Termination must invalidate pending numbering')
                    self.assertTrue(parent and not parent.IsBeingDeleted(), 'Parent destroyed inside child modal')
                else:
                    if mode == 'restore':
                        dialog.action.SetSelection(1)
                    self.button(dialog.FindWindow(wx.ID_CANCEL if mode == 'cancel' else wx.ID_OK))
            later(40, accept)
            answer = original_modal(dialog)
            self.assertGreater(state['ticks'], ticks)
            state['returned'] = True
            return answer
        plugin._modal = observe
        def completed():
            if state['returned'] and not plugin._busy and plugin._numbering_request is None:
                if mode in ('close', 'terminate', 'pending_close'):
                    self.assertIsNone(plugin._dialog)
                    self.assertFalse(parent and not parent.IsBeingDeleted())
                else:
                    self.assertEqual(parent.selected()['id'], 'a' if mode == 'selection' else 'b')
                    self.assertIs(wx.Window.FindFocus(), parent.bookList)
                finish()
            else:
                later(10, completed)
        later(1, heartbeat)
        later(10, completed)
        def timeout():
            raise AssertionError('Numbering workflow timed out')
        later(3000, timeout)
        try:
            self.app.MainLoop()
        finally:
            for timer in timers:
                timer.Stop()
        if errors:
            raise errors[0]
        self.assertEqual(state['modals'], 0 if no_modal else 1)
        if no_modal or mode in ('close', 'terminate', 'cancel'):
            controller.library.set_numbering_start.assert_not_called()
        else:
            controller.library.set_numbering_start.assert_called_once_with('b', 0 if mode == 'restore' else 2 if loaded == 'b' else 1)
        self.assertEqual(controller.method_calls, [call for call in controller.method_calls
                         if call[0].startswith('library.') or (mode == 'terminate' and call[0] == 'close')])

    def test_bookmark_labels_use_immutable_book_numbering_snapshot(self):
        book = {'id': 'b', 'title': 'Druga', 'numbering_start': 2}
        rows = [{'id': str(i), 'name': 'Fragment', 'track_index': i, 'position': 61.9}
                for i in range(3)]
        dialog = self.module.BookmarksDialog(None, self.host, book, rows)
        self.windows.append(dialog)
        self.assertEqual(dialog.bookmarkList.GetStrings(), [
            'Fragment — wstęp 1 — 0:01:01', 'Fragment — wstęp 2 — 0:01:01',
            'Fragment — odcinek 1 — 0:01:01'])
        book['numbering_start'] = 0
        dialog.refresh(rows)
        self.assertIn('wstęp 2', dialog.bookmarkList.GetString(1))

    def test_numbering_native_msaa_context_and_action_names(self):
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
        def accessible(control):
            pointer = C.c_void_p()
            self.assertEqual(oleacc.AccessibleObjectFromWindow(control.GetHandle(), 0xFFFFFFFC,
                              C.byref(iid), C.byref(pointer)), 0)
            vtable = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
            child = Variant()
            child.vt, child.value.number = 3, 0
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
        book = {'id': 'b', 'title': 'Druga', 'numbering_start': 1,
                'tracks': [{'path': 'intro.mp3'}, {'path': 'first.mp3'}]}
        dialog = self.module.NumberingDialog(None, book, 1)
        self.windows.append(dialog)
        def check():
            name, role = accessible(dialog.action)
            self.assertEqual(role, 46)  # ROLE_SYSTEM_COMBOBOX
            for value in ('Druga', 'Plik 2 z 2', 'first.mp3', 'Odcinek 1'):
                self.assertIn(value, name)
            self.assertEqual(accessible(dialog.context), ('Książka i bieżący odcinek:', 42))
        self.modal_check(dialog, check)

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    watchdog = threading.Timer(30, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        unittest.main()
    finally:
        watchdog.cancel()
