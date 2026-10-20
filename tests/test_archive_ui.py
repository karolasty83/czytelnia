"""Archive UI contracts; no access to a running NVDA or user books."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import test_plugin as plugin_fixtures
import test_explorer as explorer_fixtures

ROOT = Path(__file__).resolve().parents[1]


class ArchiveExplorerTests(unittest.TestCase):
    setUp = explorer_fixtures.ExplorerTests.setUp

    def test_selected_rar_and_7z_are_recognized_only_inside_active_folder(self):
        for extension in ('.RAR', '.7z', '.ZIP'):
            chosen = explorer_fixtures.window(10, 102, 'D:\\books')
            item = SimpleNamespace(Path='D:\\books\\Książka' + extension, IsFileSystem=True)
            chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=1, Item=lambda index: item)
            with self.subTest(extension=extension):
                result = self.module._choose_folder([chosen], 10, 102, lambda w: w.tab, selection=True)
                self.assertEqual(result, ('D:\\books', item.Path))
                item.Path = 'D:\\elsewhere\\Książka' + extension
                with self.assertRaises(self.module.ExplorerError):
                    self.module._choose_folder([chosen], 10, 102, lambda w: w.tab, selection=True)


class ArchivePluginTests(unittest.TestCase):
    setUp = plugin_fixtures.PluginTests.setUp
    tearDown = plugin_fixtures.PluginTests.tearDown
    later = plugin_fixtures.PluginTests.later

    def test_initialize_supplies_bundled_7zip_path_not_system_path(self):
        self.plugin._initialize()
        kwargs = self.modules['plugin_test.library'].Library.call_args.kwargs
        self.assertEqual(kwargs.get('sevenzip_path'), ROOT / 'addon/tools/7z.exe')

    def test_archive_picker_lists_three_formats_and_cancel_has_no_import(self):
        wx = self.modules['wx']
        wx.FD_OPEN, wx.FD_FILE_MUST_EXIST, wx.ID_OK, wx.ID_CANCEL = 1, 2, 5100, 5101
        wx.FileDialog = Mock()
        self.plugin.import_source = Mock()
        for suffix, answer in (('.rar', wx.ID_OK), ('.7z', wx.ID_OK), ('.zip', wx.ID_OK), ('.rar', wx.ID_CANCEL)):
            self.plugin.import_source.reset_mock()
            dialog = Mock()
            dialog.GetPath.return_value = str(ROOT / ('książka' + suffix))
            wx.FileDialog.return_value = dialog
            self.plugin._modal = lambda current: answer
            self.plugin.choose_zip()
            wildcard = wx.FileDialog.call_args.kwargs['wildcard']
            for pattern in ('*.zip', '*.rar', '*.7z'):
                self.assertIn(pattern, wildcard)
            self.assertEqual(wx.FileDialog.call_args.args[1], 'Wybierz archiwum książki')
            dialog.Destroy.assert_called_once()
            if answer == wx.ID_OK:
                self.plugin.import_source.assert_called_once_with(Path(dialog.GetPath()))
            else:
                self.plugin.import_source.assert_not_called()

    def test_selected_rar_and_7z_use_confirmation_not_immediate_import(self):
        self.plugin._confirm_import = Mock()
        self.plugin.import_source = Mock()
        self.plugin._run = lambda fn, success: success(fn(None))
        for suffix in ('.rar', '.7z'):
            source = ROOT / ('Książka' + suffix)
            self.modules['plugin_test.explorer'].get_selected_zip.return_value = source
            self.plugin._confirm_import.reset_mock()
            self.module.GlobalPlugin.scan_explorer(self.plugin)
            self.plugin._confirm_import.assert_called_once_with(source)
        self.plugin.import_source.assert_not_called()
        self.modules['plugin_test.library'].scan_candidates.assert_not_called()


class ArchiveBuildTests(unittest.TestCase):
    def test_7zip_executable_and_dll_are_explicit_payload(self):
        spec = importlib.util.spec_from_file_location('archive_build_test', ROOT / 'build.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = {name: path for path, name in module.payload(ROOT)}
        for name in ('7z.exe', '7z.dll'):
            self.assertEqual(payload.get('tools/' + name), ROOT / 'tools' / name)
        self.assertNotIn('tools/7zip-source.json', payload)
        self.assertIn('tools/ffprobe.exe', payload)


if __name__ == '__main__':
    unittest.main()
