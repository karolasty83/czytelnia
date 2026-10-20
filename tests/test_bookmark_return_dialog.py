"""Return shortcuts tested in real wx on the opt-in private desktop."""
import unittest
from unittest.mock import patch
import test_bookmarks_dialog as fixtures

wx = fixtures.wx


@unittest.skipUnless(fixtures.ISOLATED and wx is not None, 'Private Windows desktop required')
class ReturnDialogTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BookmarkDialogTests.setUpClass.__func__)
    setUp = fixtures.BookmarkDialogTests.setUp
    tearDown = fixtures.BookmarkDialogTests.tearDown
    library = fixtures.BookmarkDialogTests.library
    bookmarks = fixtures.BookmarkDialogTests.bookmarks
    key = fixtures.BookmarkDialogTests.key
    button = fixtures.BookmarkDialogTests.button
    modal_check = fixtures.BookmarkDialogTests.modal_check

    def test_delete_on_bookmark_list_shares_remove_button_and_ignores_repeat(self):
        window = self.bookmarks()
        def check():
            window.bookmarkList.SetSelection(1)
            window.bookmarkList.SetFocus()
            self.key(window, wx.WXK_DELETE)
            self.host.remove_bookmark.assert_called_once_with(window, 'y')
            self.key(window, wx.WXK_DELETE, repeat=True)
            self.host.remove_bookmark.assert_called_once_with(window, 'y')
            self.button(window.removeButton)
            self.assertEqual(self.host.remove_bookmark.call_args_list,
                             [((window, 'y'),), ((window, 'y'),)])
        self.modal_check(window, check)

    def test_delete_with_modifier_other_focus_or_empty_list_does_not_remove(self):
        window = self.bookmarks()
        def check():
            window.bookmarkList.SetFocus()
            for params in ({'ctrl': True}, {'shift': True}, {'alt': True}):
                self.key(window, wx.WXK_DELETE, **params)
            window.renameButton.SetFocus()
            self.key(window, wx.WXK_DELETE)
            window.refresh([])
            window.bookmarkList.SetFocus()
            self.key(window, wx.WXK_DELETE)
            self.host.remove_bookmark.assert_not_called()
        self.modal_check(window, check)

    def test_library_return_shortcut_and_context_menu_use_selected_book(self):
        window = self.library()
        window.bookList.SetSelection(1)
        self.key(window, ord('B'), ctrl=True, shift=True)
        self.host.return_from_bookmark.assert_called_once_with('b')
        def popup(menu):
            item = next(i for i in menu.GetMenuItems() if i.GetItemLabel().startswith('Wróć do słuchania'))
            menu.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, item.GetId()))
        with patch.object(window.bookList, 'PopupMenu', side_effect=popup):
            event = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU)
            event.SetPosition(wx.DefaultPosition)
            window.on_context_menu(event)
        self.assertEqual(self.host.return_from_bookmark.call_args_list, [(('b',),), (('b',),)])
        self.host.start_book.assert_not_called()
        self.host.show_bookmarks.assert_not_called()

    def test_bookmarks_return_shortcut_targets_dialog_book_only(self):
        window = self.bookmarks()
        def check():
            window.bookmarkList.SetSelection(1)
            window.bookmarkList.SetFocus()
            self.key(window, ord('B'), ctrl=True, shift=True)
            self.key(window, ord('B'), ctrl=True, shift=True, repeat=True)
            self.host.return_from_bookmark.assert_called_once_with('b', window)
            self.host.start_bookmark.assert_not_called()
        self.modal_check(window, check)

    def test_library_repeat_extra_modifiers_and_foreign_focus_never_return(self):
        window = self.library()
        for params in ({'repeat': True}, {'alt': True}):
            self.key(window, ord('B'), ctrl=True, shift=True, **params)
        with patch.object(window, 'FindFocus', return_value=None):
            event = self.key(window, ord('B'), ctrl=True, shift=True)
            self.assertTrue(event.GetSkipped())
        self.host.return_from_bookmark.assert_not_called()

    def test_return_suppresses_held_seek_resume_and_empty_library_has_no_action(self):
        window = self.library()
        with patch.object(window, 'stop_adjustment') as stop:
            self.key(window, ord('B'), ctrl=True, shift=True)
            stop.assert_called_once_with(finish=False)
        self.host.return_from_bookmark.reset_mock()
        window.refresh([])
        self.assertIsNone(window.selected())
        self.key(window, ord('B'), ctrl=True, shift=True)
        self.host.return_from_bookmark.assert_not_called()


if __name__ == '__main__':
    unittest.main()
