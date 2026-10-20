"""Native wx archive controls, private non-input desktop only."""
from pathlib import Path
import unittest
import test_bookmarks_dialog as fixtures

wx = fixtures.wx


@unittest.skipUnless(fixtures.ISOLATED and wx is not None, 'Private Windows desktop required')
class ArchiveDialogTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BookmarkDialogTests.setUpClass.__func__)
    setUp = fixtures.BookmarkDialogTests.setUp
    tearDown = fixtures.BookmarkDialogTests.tearDown
    library = fixtures.BookmarkDialogTests.library
    button = fixtures.BookmarkDialogTests.button

    def test_add_archive_button_has_new_label_and_existing_action(self):
        window = self.library()
        self.assertEqual(window.zipButton.GetLabel(), 'Dodaj archiwum')
        self.button(window.zipButton)
        self.host.choose_zip.assert_called_once_with()
        window.refresh([])
        self.assertTrue(window.zipButton.IsShown())
        self.assertTrue(window.zipButton.IsEnabled())

    def test_candidates_identify_rar_7z_and_zip_not_as_folders(self):
        paths = [Path(name) for name in ('Tom.rar', 'Tom.7z', 'Tom.zip', 'Folder')]
        window = self.module.CandidateDialog(None, paths)
        self.windows.append(window)
        self.assertEqual(window.candidates.GetStrings(),
                         ['Tom.rar — RAR', 'Tom.7z — 7z', 'Tom.zip — ZIP', 'Folder — folder'])
        for index, path in enumerate(paths):
            window.candidates.SetSelection(index)
            self.assertEqual(window.selected_path(), path)


if __name__ == '__main__':
    unittest.main()
