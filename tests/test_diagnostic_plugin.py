"""Plugin integration without NVDA or the real library."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_plugin


class DiagnosticPluginTests(unittest.TestCase):
    setUp = test_plugin.PluginTests.setUp
    tearDown = test_plugin.PluginTests.tearDown
    later = test_plugin.PluginTests.later

    def test_player_callback_creates_log_only_on_error_in_library(self):
        with tempfile.TemporaryDirectory(dir=test_plugin.ROOT / '.test-work') as directory:
            with patch.dict('os.environ', {'LOCALAPPDATA': directory}):
                self.plugin._initialize()
                controller = self.modules['plugin_test.controller'].Controller
                factory = controller.call_args.args[1]
                factory(lambda *args: None)
                kwargs = self.modules['plugin_test.player'].Player.call_args.kwargs
                self.assertIn('diagnostic', kwargs)
                folder = Path(directory) / 'Czytelnia' / 'biblioteka'
                self.assertFalse((folder / 'czytelnia.log').exists())
                kwargs['diagnostic']('playback.start.volume', 'WinError 10053 / reply_body')
                text = (folder / 'czytelnia.log').read_text(encoding='utf-8')
                self.assertIn('10053', text)
                self.assertIn('reply_body', text)

    def test_operation_exception_records_traceback_and_keeps_message(self):
        with tempfile.TemporaryDirectory(dir=test_plugin.ROOT / '.test-work') as directory:
            with patch.dict('os.environ', {'LOCALAPPDATA': directory}):
                def fail(controller):
                    raise OSError('diagnostic-test-failure')
                self.assertTrue(self.plugin._run(fail))
                self.plugin._executor.submit(lambda: None).result(timeout=5)
                path = Path(directory) / 'Czytelnia/biblioteka/czytelnia.log'
                self.assertTrue(path.is_file())
                text = path.read_text(encoding='utf-8')
                self.assertIn('Traceback', text)
                self.assertIn('diagnostic-test-failure', text)
                self.modules['ui'].message.assert_called()

if __name__ == '__main__':
    unittest.main()
