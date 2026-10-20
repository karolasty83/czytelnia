"""Bookmark orchestration: fake NVDA/wx, real serialized worker; no speech/UI."""
import queue
import threading
import unittest
from unittest.mock import Mock
import test_plugin as harness


class BookmarkPluginTests(unittest.TestCase):
    later = harness.PluginTests.later
    tearDown = harness.PluginTests.tearDown

    def setUp(self):
        harness.PluginTests.setUp(self)
        self.ui_queue = queue.Queue()
        self.modules['wx'].CallAfter = lambda fn, *a, **kw: self.ui_queue.put((fn, a, kw))
        self.controller = self.plugin._controller = Mock()
        self.parent = self.plugin._dialog = Mock()
        self.parent.IsBeingDeleted.return_value = False
        self.parent.selected.return_value = {'id': 'selected', 'title': 'Wybrana'}
        self.parent.books = [self.parent.selected.return_value]
        self.controller.book_id = 'playing'
        self.rows = [{'id': 'mark', 'name': 'Zakładka1', 'track_index': 2, 'position': 3723.5}]
        self.controller.library.get_book.return_value = self.parent.selected.return_value
        self.controller.library.bookmarks.return_value = self.rows
        self.modules['plugin_test.dialogs'].BookmarksDialog = Mock()
        self.window = self.modules['plugin_test.dialogs'].BookmarksDialog.return_value
        self.window.IsBeingDeleted.return_value = False
        self.window.book_id = 'selected'
        self.parent.selected.reset_mock()

    def drain(self):
        while not self.ui_queue.empty():
            fn, a, kw = self.ui_queue.get_nowait()
            fn(*a, **kw)

    def finish(self):
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self.drain()

    def test_add_uses_loaded_controller_not_library_selection(self):
        self.assertTrue(hasattr(self.plugin, 'add_bookmark'), 'Missing bookmark action')
        owner = self.plugin._executor.submit(threading.get_ident).result(timeout=5)
        self.controller.add_bookmark.side_effect = lambda: self.assertEqual(threading.get_ident(), owner)
        self.plugin.add_bookmark()
        self.finish()
        self.controller.add_bookmark.assert_called_once_with()
        self.parent.selected.assert_not_called()
        self.modules['ui'].message.assert_not_called()  # Controller owns announcement.
    def test_open_reads_selected_book_on_worker_defers_modal_and_restores_focus(self):
        self.assertTrue(hasattr(self.plugin, 'show_bookmarks'), 'Missing bookmarks list')
        owner = self.plugin._executor.submit(threading.get_ident).result(timeout=5)
        self.controller.library.bookmarks.side_effect = lambda key: (
            self.assertEqual(threading.get_ident(), owner) or self.rows)
        self.plugin._modal = Mock(return_value=5101)
        self.plugin.show_bookmarks('selected')
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self.plugin._modal.assert_not_called()
        self.drain()
        self.controller.library.bookmarks.assert_called_once_with('selected')
        factory = self.modules['plugin_test.dialogs'].BookmarksDialog
        factory.assert_called_once_with(self.parent, self.plugin, self.parent.selected.return_value, self.rows)
        self.plugin._modal.assert_called_once_with(self.window)
        self.window.Destroy.assert_called_once()
        self.parent.bookList.SetFocus.assert_called_once()
        self.controller.start.assert_not_called()
        self.controller.player.pause.assert_not_called()

    def test_empty_list_speaks_exact_message_without_window(self):
        self.assertTrue(hasattr(self.plugin, 'show_bookmarks'), 'Missing bookmarks list')
        self.controller.library.bookmarks.return_value = []
        self.plugin.show_bookmarks('selected')
        self.finish()
        self.modules['ui'].message.assert_called_once_with('brak zakładek')
        self.modules['plugin_test.dialogs'].BookmarksDialog.assert_not_called()
    def test_open_requests_coalesce_and_late_close_cancels_request(self):
        self.plugin._modal = Mock()
        self.plugin.show_bookmarks('selected')
        self.plugin.show_bookmarks('selected')
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self.plugin.dialog_closed(self.parent)
        self.assertIsNone(self.plugin._bookmark_request)
        self.drain()
        self.controller.library.bookmarks.assert_called_once()
        self.plugin._modal.assert_not_called()

    def test_changed_selection_or_deleted_book_never_opens_stale_list(self):
        self.plugin._modal = Mock()
        self.plugin.show_bookmarks('selected')
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self.parent.selected.return_value = {'id': 'other'}
        self.drain()
        self.plugin._modal.assert_not_called()
        self.parent.selected.return_value = {'id': 'selected'}
        self.controller.library.get_book.return_value = None
        self.plugin.show_bookmarks('selected')
        self.finish()
        self.plugin._modal.assert_not_called()

    def live_window(self):
        self.plugin._bookmark_dialog = self.window
        self.window._closed = False
        self.window.GetParent.return_value = self.parent
        self.window.focus_bookmark.side_effect = lambda key: self.window.bookmarkList.SetFocus()

    def test_enter_starts_only_captured_bookmark_on_worker_then_closes(self):
        self.assertTrue(hasattr(self.plugin, 'start_bookmark'), 'Missing bookmark playback')
        self.live_window()
        owner = self.plugin._executor.submit(threading.get_ident).result(timeout=5)
        self.controller.start_bookmark.side_effect = lambda *args: self.assertEqual(threading.get_ident(), owner)
        self.plugin.start_bookmark(self.window, 'mark')
        self.finish()
        self.controller.start_bookmark.assert_called_once_with('selected', 'mark')
        self.window.on_close.assert_called_once_with()
        self.controller.start.assert_not_called()

    def test_terminate_closes_bookmark_modal_and_cancels_pending_open(self):
        self.live_window()
        self.window.IsModal.return_value = True
        self.modules['wx'].ID_CANCEL = 5101
        self.plugin._bookmark_request = object()
        self.plugin.terminate()
        self.assertIsNone(self.plugin._bookmark_request)
        self.window.on_close.assert_called_once_with()
    def prompt_setup(self, answer):
        self.live_window()
        wx = self.modules['wx']
        wx.ID_OK, wx.ID_CANCEL, wx.ID_YES, wx.ID_NO = 5100, 5101, 5103, 5104
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_WARNING = 1, 2, 4
        wx.MessageDialog = Mock()
        self.modules['plugin_test.dialogs'].BookmarkNameDialog = Mock()
        self.prompt = self.modules['plugin_test.dialogs'].BookmarkNameDialog.return_value
        self.prompt.bookmark_name.return_value = 'Moja nazwa'
        self.controller.library.get_bookmark.return_value = self.rows[0]
        self.plugin._modal = Mock(return_value=answer)

    def test_rename_captures_id_prefills_and_writes_only_after_ok_on_worker(self):
        self.assertTrue(hasattr(self.plugin, 'rename_bookmark'), 'Missing bookmark rename')
        self.prompt_setup(5100)
        owner = self.plugin._executor.submit(threading.get_ident).result(timeout=5)
        self.controller.library.rename_bookmark.side_effect = lambda *a: self.assertEqual(threading.get_ident(), owner)
        self.plugin.rename_bookmark(self.window, 'mark')
        self.plugin._modal.assert_not_called()
        self.finish()
        self.finish()
        self.modules['plugin_test.dialogs'].BookmarkNameDialog.assert_called_once_with(self.window, 'Zakładka1')
        self.controller.library.rename_bookmark.assert_called_once_with('selected', 'mark', 'Moja nazwa')
        self.window.refresh.assert_called_once_with(self.rows, selected_id='mark')
        self.window.bookmarkList.SetFocus.assert_called()
        self.prompt.Destroy.assert_called_once()
        self.controller.player.pause.assert_not_called()
        self.controller.save.assert_not_called()

    def test_rename_cancel_does_not_write_and_returns_list_focus(self):
        self.assertTrue(hasattr(self.plugin, 'rename_bookmark'), 'Missing bookmark rename')
        self.prompt_setup(5101)
        self.plugin.rename_bookmark(self.window, 'mark')
        self.finish()
        self.controller.library.rename_bookmark.assert_not_called()
        self.window.bookmarkList.SetFocus.assert_called_once()

    def test_delete_defaults_to_no_and_cancel_never_writes(self):
        self.assertTrue(hasattr(self.plugin, 'remove_bookmark'), 'Missing bookmark removal')
        self.prompt_setup(5104)
        self.plugin.remove_bookmark(self.window, 'mark')
        self.plugin._modal.assert_not_called()
        self.finish()
        wx = self.modules['wx']
        self.assertTrue(wx.MessageDialog.call_args.kwargs['style'] & wx.NO_DEFAULT)
        self.assertIn('Zakładka1', wx.MessageDialog.call_args.args[1])
        self.controller.library.remove_bookmark.assert_not_called()
        self.window.bookmarkList.SetFocus.assert_called_once()

    def test_delete_last_closes_and_speaks_exactly_without_playback_changes(self):
        self.assertTrue(hasattr(self.plugin, 'remove_bookmark'), 'Missing bookmark removal')
        self.prompt_setup(5103)
        self.controller.library.bookmarks.return_value = []
        self.plugin.remove_bookmark(self.window, 'mark')
        self.finish()
        self.finish()
        self.controller.library.remove_bookmark.assert_called_once_with('selected', 'mark')
        self.window.on_close.assert_called_once()
        self.modules['ui'].message.assert_called_once_with('brak zakładek')
        self.controller.player.pause.assert_not_called()
        self.controller.remove.assert_not_called()
        self.controller.save.assert_not_called()
    def test_termination_during_nested_prompt_cancels_it_and_never_writes(self):
        self.prompt_setup(5100)
        self.prompt.IsModal.return_value = True
        def modal(prompt):
            self.plugin.terminate()
            return 5100  # A late OK must not resurrect the accepted action.
        self.plugin._modal.side_effect = modal
        self.plugin.rename_bookmark(self.window, 'mark')
        self.finish()
        self.prompt.EndModal.assert_called_once_with(5101)
        self.controller.library.rename_bookmark.assert_not_called()
        self.assertIsNone(self.plugin._bookmark_edit_token)

    def test_pending_rename_coalesces_and_closed_window_suppresses_prompt(self):
        self.prompt_setup(5100)
        self.plugin.rename_bookmark(self.window, 'mark')
        self.plugin.rename_bookmark(self.window, 'mark')
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self.window._closed = True
        self.drain()
        self.controller.library.get_bookmark.assert_called_once_with('selected', 'mark')
        self.plugin._modal.assert_not_called()
        self.assertIsNone(self.plugin._bookmark_edit_token)
        self.controller.library.rename_bookmark.assert_not_called()

    def test_success_after_list_closed_does_not_touch_dead_controls(self):
        self.prompt_setup(5100)
        entered, release = threading.Event(), threading.Event()
        def blocked_write(*args):
            entered.set()
            self.assertTrue(release.wait(5), 'Test did not release bookmark write')
        self.controller.library.rename_bookmark.side_effect = blocked_write
        try:
            self.plugin.rename_bookmark(self.window, 'mark')
            self.finish()  # Deliver preflight; mutation is now blocked on the worker.
            self.assertTrue(entered.wait(5))
            self.window.bookmarkList.SetFocus.reset_mock()
            self.window._closed = True
        finally:
            release.set()
        self.finish()
        self.window.refresh.assert_not_called()
        self.window.bookmarkList.SetFocus.assert_not_called()

    def test_remove_book_confirmation_mentions_progress_and_bookmarks(self):
        wx = self.modules['wx']
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_WARNING, wx.YES = 1, 2, 4, 8
        self.modules['gui'].messageBox = Mock(return_value=0)
        self.plugin.remove_book('selected')
        self.assertEqual(self.modules['gui'].messageBox.call_args.args[0],
            'Usunąć kopię książki z biblioteki wraz z postępem i zakładkami?')
        self.controller.remove.assert_not_called()
    def test_rename_cancel_restores_captured_id_after_browsing_during_read(self):
        self.prompt_setup(5101)
        self.window.selected.return_value = {'id': 'other'}
        self.plugin.rename_bookmark(self.window, 'mark')
        self.finish()
        self.window.focus_bookmark.assert_called_once_with('mark')

    def test_start_is_ignored_while_rename_prompt_is_pending(self):
        self.prompt_setup(5101)
        self.plugin.rename_bookmark(self.window, 'mark')
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        fn, a, kw = self.ui_queue.get_nowait()
        fn(*a, **kw)  # Read finished, busy reset, modal still deferred.
        self.plugin.start_bookmark(self.window, 'mark')
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self.controller.start_bookmark.assert_not_called()
        self.drain()
    def test_removed_book_closes_its_open_bookmarks(self):
        self.live_window()
        wx = self.modules['wx']
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_WARNING, wx.YES = 1, 2, 4, 8
        self.modules['gui'].messageBox = Mock(return_value=wx.YES)
        self.controller.library.books.return_value = []
        self.plugin.remove_book('selected')
        self.finish()
        self.window.on_close.assert_called_once_with()
    def test_real_core_round_trip_preserves_progress_other_book_and_exact_position(self):
        import importlib.util
        from pathlib import Path
        import tempfile
        from test_controller import Player
        def load(name, file):
            spec = importlib.util.spec_from_file_location(name, harness.FILE.with_name(file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        library_module = load('bookmarks_plugin_real_library', 'library.py')
        controller_module = load('bookmarks_plugin_real_controller', 'controller.py')
        with tempfile.TemporaryDirectory(dir=harness.ROOT / '.test-work') as folder:
            root = Path(folder)
            library = library_module.Library(root / 'config', duration_probe=lambda path: 120)
            books = []
            for title in ('Słuchana', 'Wybrana'):
                source = root / title
                source.mkdir()
                for index in range(2):
                    (source / f'{index}.mp3').write_bytes(b'synthetic fixture, no real audio')
                books.append(library.import_book(source))
            playing, selected = [b['id'] for b in books]
            controller = self.plugin._controller = controller_module.Controller(
                library, Player, self.plugin._dispatch_event, self.plugin._say,
                cancelled=lambda: self.plugin._closed)
            worker = self.plugin._executor
            worker.submit(controller.start, playing, 1).result(timeout=5)
            controller.player.position = 61.25
            before_progress = library.get_book(playing)
            self.parent.books = books
            self.parent.selected.return_value = books[1]
            self.drain()
            self.modules['ui'].message.reset_mock()
            self.plugin.add_bookmark()
            self.finish()
            mark = library.bookmarks(playing)[0]
            self.assertEqual((mark['track_index'], mark['position']), (1, 61.25))
            self.assertEqual(library.bookmarks(selected), [])
            self.assertEqual(library.get_book(playing)['position'], before_progress['position'])
            self.assertTrue(controller.player.is_playing)
            worker.submit(controller.toggle, playing).result(timeout=5)
            self.assertFalse(controller.player.is_playing)
            controller.player.position = 85.5
            self.plugin.add_bookmark()
            self.finish()
            self.assertEqual(len(library.bookmarks(playing)), 2)
            self.assertFalse(controller.player.is_playing)
            progress = {key: (library.get_book(key)['track_index'], library.get_book(key)['position'])
                        for key in (playing, selected)}
            self.prompt_setup(5100)
            self.window.book_id = playing
            self.plugin.rename_bookmark(self.window, mark['id'])
            self.finish()
            self.finish()
            self.assertEqual(library.get_bookmark(playing, mark['id'])['name'], 'Moja nazwa')
            self.assertFalse(controller.player.is_playing)
            self.plugin._modal.return_value = 5103
            second = library.bookmarks(playing)[1]
            self.plugin.remove_bookmark(self.window, second['id'])
            self.finish()
            self.finish()
            self.assertEqual(len(library.bookmarks(playing)), 1)
            self.assertFalse(controller.player.is_playing)
            for key in (playing, selected):
                self.assertEqual((library.get_book(key)['track_index'], library.get_book(key)['position']), progress[key])
            self.plugin.start_bookmark(self.window, mark['id'])
            self.finish()
            self.assertTrue(controller.player.is_playing)
            self.assertEqual((controller.book_id, controller.track_index, controller.player.position),
                             (playing, 1, 61.25))
            restored = library_module.Library(root / 'config')
            self.assertEqual(restored.get_bookmark(playing, mark['id'])['name'], 'Moja nazwa')
            self.assertEqual(restored.bookmarks(selected), [])
            worker.submit(controller.close).result(timeout=5)
            self.plugin._controller = None  # Temporary data removed only after close.
    def test_busy_and_failed_reads_release_gate_for_retry_without_empty_dialog(self):
        self.plugin._record_exception = Mock()
        self.plugin._busy = True
        self.plugin.show_bookmarks('selected')
        self.assertIsNone(self.plugin._bookmark_request)
        self.plugin._busy = False
        self.controller.library.get_book.side_effect = OSError('synthetic library read failure')
        self.plugin.show_bookmarks('selected')
        self.finish()
        self.assertIsNone(self.plugin._bookmark_request)
        self.assertFalse(self.plugin._busy)
        self.modules['plugin_test.dialogs'].BookmarksDialog.assert_not_called()
        self.controller.library.get_book.side_effect = None
        self.plugin._modal = Mock(return_value=5101)
        self.plugin.show_bookmarks('selected')
        self.finish()
        self.plugin._modal.assert_called_once_with(self.window)

    def test_write_failure_keeps_list_and_releases_busy_without_false_success(self):
        self.prompt_setup(5100)
        self.plugin._record_exception = Mock()
        self.controller.library.rename_bookmark.side_effect = OSError('synthetic write failure')
        self.plugin.rename_bookmark(self.window, 'mark')
        self.finish()
        self.finish()
        self.assertFalse(self.plugin._busy)
        self.assertIsNone(self.plugin._bookmark_edit_token)
        self.window.refresh.assert_not_called()
        self.window.on_close.assert_not_called()
        self.modules['ui'].message.assert_called_once_with(
            'Nie udało się wykonać operacji. synthetic write failure')


if __name__ == '__main__':
    unittest.main()
