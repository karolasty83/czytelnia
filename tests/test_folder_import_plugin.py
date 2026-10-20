"""Selected folders are scoped before scanning; no sibling traversal/import."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import test_plugin

ROOT = Path(__file__).resolve().parents[1]


class FolderPluginTests(unittest.TestCase):
    setUp = test_plugin.PluginTests.setUp
    tearDown = test_plugin.PluginTests.tearDown
    later = test_plugin.PluginTests.later

    def prepare(self):
        spec = importlib.util.spec_from_file_location('scoped_library',
            ROOT / 'addon/globalPlugins/czytelnia/library.py')
        library = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(library)
        self.module.scan_candidates = library.scan_candidates
        self.module.FolderSelectionRequired = library.FolderSelectionRequired
        self.plugin._confirm_import = Mock()
        self.plugin._show_candidates = Mock()
        self.plugin.import_source = Mock()
        self.plugin._say = Mock()
        self.plugin._run = lambda fn, success: success(fn(None))
        work = ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='selected-folder-', dir=work)
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.source = self.parent / 'Książka. Tom 2'
        self.source.mkdir()
        self.modules['plugin_test.explorer'].get_current_folder.return_value = self.parent
        self.modules['plugin_test.explorer'].get_selected_source.return_value = self.source
        return library

    def test_selected_folder_ignores_siblings_and_confirms_its_full_name(self):
        library = self.prepare()
        (self.source / '1.mp3').write_bytes(b'audio')
        (self.parent / 'other.bin').write_bytes(b'x' * 100)
        other = self.parent / 'Inna książka'
        other.mkdir()
        (other / '2.mp3').write_bytes(b'other audio')
        with patch.object(library, 'MAX_FILE_BYTES', 10):
            self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._confirm_import.assert_called_once_with(self.source, is_folder=True)
        self.plugin._show_candidates.assert_not_called()
        self.plugin.import_source.assert_not_called()
        self.assertGreaterEqual(self.modules['plugin_test.explorer'].get_selected_source.call_count, 2)

    def test_selected_folder_with_one_or_two_children_only_speaks(self):
        self.prepare()
        child = self.source / 'Pierwsza'
        child.mkdir()
        (child / '1.mp3').write_bytes(b'nested')
        for count, noun in ((1, 'podfolder'), (2, 'podfoldery')):
            if count == 2:
                (self.source / 'Druga').mkdir()
            self.plugin._say.reset_mock()
            self.module.GlobalPlugin.scan_explorer(self.plugin)
            self.plugin._say.assert_called_once_with(
                f'Folder zawiera {noun}. Otwórz go i zaznacz folder z nagraniami.')
        self.plugin._confirm_import.assert_not_called()
        self.plugin._show_candidates.assert_not_called()
        self.plugin.import_source.assert_not_called()

    def test_open_parent_with_nested_audio_only_speaks(self):
        self.prepare()
        (self.source / '1.mp3').write_bytes(b'nested')
        self.modules['plugin_test.explorer'].get_selected_source.return_value = None
        self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._say.assert_called_once_with(
            'Folder zawiera podfolder. Otwórz go i zaznacz folder z nagraniami.')
        self.plugin._confirm_import.assert_not_called()
        self.plugin._show_candidates.assert_not_called()
        self.plugin.import_source.assert_not_called()

    def test_selected_empty_folder_does_not_offer_sibling_archives(self):
        self.prepare()
        (self.parent / 'other.zip').write_bytes(b'not the selected source')
        self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._say.assert_called_once_with('Nie znaleziono plików dźwiękowych w źródle.')
        self.plugin._confirm_import.assert_not_called()
        self.plugin._show_candidates.assert_not_called()

    def test_selection_change_during_scan_refuses_confirmation(self):
        self.prepare()
        (self.source / '1.mp3').write_bytes(b'audio')
        self.modules['plugin_test.explorer'].get_selected_source.side_effect = [self.source, None]
        with self.assertRaisesRegex(RuntimeError, 'zmieni'):
            self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._confirm_import.assert_not_called()

    def test_selection_change_also_invalidates_subfolder_message(self):
        self.prepare()
        (self.source / 'child').mkdir()
        self.modules['plugin_test.explorer'].get_selected_source.side_effect = [self.source, None]
        with self.assertRaisesRegex(RuntimeError, 'zmieni'):
            self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._say.assert_not_called()

    def test_new_selection_during_open_folder_scan_is_not_ignored(self):
        self.prepare()
        (self.parent / 'direct.mp3').write_bytes(b'direct')
        self.modules['plugin_test.explorer'].get_selected_source.side_effect = [None, self.source]
        with self.assertRaisesRegex(RuntimeError, 'zmieni'):
            self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._confirm_import.assert_not_called()


if __name__ == '__main__':
    unittest.main()
