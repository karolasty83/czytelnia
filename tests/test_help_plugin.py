"""Help uses NVDA's documentation resolver, never a global F1 gesture."""
import sys
import types
import unittest
from unittest.mock import Mock, patch
import test_plugin

class HelpPluginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_plugin.PluginTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.p = self.fixture.plugin
        self.module = self.fixture.module
        self.p._say = Mock()

    def test_help_opens_resolved_document_without_initializing_playback(self):
        self.assertTrue(hasattr(self.p, 'show_help'), 'Missing help action')
        document = str(test_plugin.ROOT / 'Czytelnia-podrecznik.html')
        addon = Mock()
        addon.getDocFilePath.return_value = document
        handler = types.SimpleNamespace(getCodeAddon=Mock(return_value=addon))
        with patch.dict(sys.modules, {'addonHandler': handler}), \
             patch.object(self.module.os, 'startfile', create=True) as launch, \
             patch.object(self.p, '_initialize') as initialize:
            self.p.show_help()
            addon.getDocFilePath.assert_called_once_with()
            launch.assert_called_once_with(document)
            initialize.assert_not_called()
            self.p._say.assert_not_called()
        gestures = self.module.GlobalPlugin._GlobalPlugin__gestures
        self.assertFalse(any('f1' in gesture.lower() for gesture in gestures))

    def test_missing_document_and_open_error_are_spoken(self):
        self.assertTrue(hasattr(self.p, 'show_help'), 'Missing help action')
        document = str(test_plugin.ROOT / 'Czytelnia-podrecznik.html')
        addon = Mock()
        handler = types.SimpleNamespace(getCodeAddon=Mock(return_value=addon))
        for path, error in ((None, None), (document + '.missing', None),
                            (document, OSError('Brak skojarzenia HTML'))):
            with self.subTest(path=path, error=error), \
                 patch.dict(sys.modules, {'addonHandler': handler}), \
                 patch.object(self.module.os, 'startfile', create=True, side_effect=error) as launch:
                self.p._say.reset_mock()
                addon.getDocFilePath.return_value = path
                self.p.show_help()
                self.p._say.assert_called_once()
                self.assertIn('Nie można otworzyć podręcznika', self.p._say.call_args.args[0])
                if not error:
                    launch.assert_not_called()

    def test_closed_plugin_does_not_open_help(self):
        self.assertTrue(hasattr(self.p, 'show_help'), 'Missing help action')
        self.p._closed = True
        with patch.object(self.module.os, 'startfile', create=True) as launch:
            self.p.show_help()
            launch.assert_not_called()

if __name__ == '__main__':
    unittest.main()
