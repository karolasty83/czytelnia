"""Return-point UI orchestration, isolated fake NVDA and real worker."""
import threading
import unittest
import test_bookmarks_plugin as fixtures


class ReturnPluginTests(unittest.TestCase):
    setUp = fixtures.BookmarkPluginTests.setUp
    tearDown = fixtures.BookmarkPluginTests.tearDown
    later = fixtures.BookmarkPluginTests.later
    drain = fixtures.BookmarkPluginTests.drain
    finish = fixtures.BookmarkPluginTests.finish
    live_window = fixtures.BookmarkPluginTests.live_window

    def test_return_runs_on_worker_for_captured_book_without_changing_selection(self):
        self.assertTrue(hasattr(self.plugin, 'return_from_bookmark'), 'Missing return action')
        owner = self.plugin._executor.submit(threading.get_ident).result(timeout=5)
        def restore(book_id):
            self.assertEqual(threading.get_ident(), owner)
            return True
        self.controller.return_from_bookmark.side_effect = restore
        self.plugin.return_from_bookmark('selected')
        self.finish()
        self.controller.return_from_bookmark.assert_called_once_with('selected')
        self.parent.selected.assert_not_called()
        self.controller.start.assert_not_called()

    def test_return_closes_only_live_modal_on_success_not_absence_or_failure(self):
        self.live_window()
        self.controller.return_from_bookmark.return_value = False
        self.plugin.return_from_bookmark('selected', self.window)
        self.finish()
        self.window.on_close.assert_not_called()
        self.controller.return_from_bookmark.side_effect = OSError('test failure')
        self.plugin.return_from_bookmark('selected', self.window)
        self.finish()
        self.window.on_close.assert_not_called()
        self.controller.return_from_bookmark.side_effect = None
        self.controller.return_from_bookmark.return_value = True
        self.plugin.return_from_bookmark('selected', self.window)
        self.finish()
        self.window.on_close.assert_called_once_with()

    def test_return_ignores_stale_modal_and_nested_edit(self):
        self.live_window()
        self.window._closed = True
        self.plugin.return_from_bookmark('selected', self.window)
        self.finish()
        self.controller.return_from_bookmark.assert_not_called()
        self.window._closed = False
        self.plugin._bookmark_edit_token = object()
        self.plugin.return_from_bookmark('selected', self.window)
        self.finish()
        self.controller.return_from_bookmark.assert_not_called()

    def test_return_does_not_resume_held_seek_before_jump(self):
        from unittest.mock import Mock
        session = Mock()
        session.allow_resume = True
        self.plugin._adjust_sessions = {'seek_relative': session}
        self.plugin.return_from_bookmark('selected')
        self.finish()
        self.assertFalse(session.allow_resume)
        session.release.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
