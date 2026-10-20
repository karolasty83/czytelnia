"""Every OS power boundary is mocked: these tests never change host power."""
import ctypes
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/power.py'


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), 'power.py must exist')
        spec = importlib.util.spec_from_file_location('power_test', MODULE)
        self.module = importlib.util.module_from_spec(spec)
        # Guard against accidental subprocess/DLL work even during import.
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('real subprocess forbidden')):
            with mock.patch.object(ctypes, 'WinDLL', create=True,
                                   side_effect=AssertionError('real DLL forbidden')):
                spec.loader.exec_module(self.module)
        self.run = mock.Mock(return_value=SimpleNamespace(returncode=0))
        self.suspend = mock.Mock(return_value=1)
        self.load = mock.Mock(return_value=SimpleNamespace(SetSuspendState=self.suspend))
        self.error = OSError('Windows power failure')
        self.win_error = mock.Mock(return_value=self.error)
        # Patch every route before any perform call, independent of the host OS.
        for name, value in (('_run', self.run), ('_load_powrprof', self.load),
                            ('_win_error', self.win_error),
                            ('os', SimpleNamespace(name='nt', environ={'SystemRoot': r'C:\Windows'}))):
            patcher = mock.patch.object(self.module, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_shutdown_is_absolute_non_forcing_and_shell_free(self):
        self.assertIsNone(self.module.perform('shutdown'))
        self.run.assert_called_once_with(
            [r'C:\Windows\System32\shutdown.exe', '/s', '/t', '0'],
            shell=False, check=True, timeout=15,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.load.assert_not_called()
        self.suspend.assert_not_called()

    def test_rejects_unknown_actions_before_any_os_work(self):
        for action in ('stop', 'hibernate', 'logoff', '', 'sleep /f', 'SHUTDOWN',
                       None, True, 1, [], {}):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    self.module.perform(action)
        self.run.assert_not_called()
        self.load.assert_not_called()

    def test_rejects_non_windows_hosts_before_any_os_work(self):
        for name in ('posix', 'java', ''):
            self.module.os.name = name
            for action in ('shutdown', 'sleep'):
                with self.subTest(os=name, action=action):
                    with self.assertRaises(OSError):
                        self.module.perform(action)
        self.run.assert_not_called()
        self.load.assert_not_called()

    def test_rejects_missing_or_nonabsolute_system_root(self):
        for root in (None, '', 'Windows', r'C:Windows', r'\Windows', 'C:\x00Windows'):
            self.module.os.environ = {} if root is None else {'SystemRoot': root}
            with self.subTest(root=root):
                with self.assertRaises(OSError):
                    self.module.perform('shutdown')
        self.run.assert_not_called()
        self.load.assert_not_called()

    def test_shutdown_errors_and_timeouts_propagate(self):
        for error in (subprocess.CalledProcessError(1, 'mock-shutdown'),
                      subprocess.TimeoutExpired('mock-shutdown', 15),
                      OSError('mock executable unavailable')):
            self.run.side_effect = error
            with self.subTest(error=error):
                with self.assertRaises(type(error)) as raised:
                    self.module.perform('shutdown')
                self.assertIs(raised.exception, error)
        self.load.assert_not_called()

    def test_system_root_with_spaces_is_one_argument(self):
        self.module.os.environ['SystemRoot'] = r'D:\Windows Space'
        self.module.perform('shutdown')
        self.assertEqual(self.run.call_args.args[0],
                         [r'D:\Windows Space\System32\shutdown.exe', '/s', '/t', '0'])

    def test_sleep_calls_typed_boolean_api_without_hibernating_or_forcing(self):
        self.assertIsNone(self.module.perform('sleep'))
        self.load.assert_called_once_with()
        self.assertEqual(self.suspend.argtypes, [ctypes.c_ubyte] * 3)
        self.assertIs(self.suspend.restype, ctypes.c_ubyte)
        self.suspend.assert_called_once_with(False, False, False)
        self.run.assert_not_called()
        self.win_error.assert_not_called()

    def test_sleep_api_failure_raises_windows_error(self):
        self.suspend.return_value = 0
        with self.assertRaises(OSError) as raised:
            self.module.perform('sleep')
        self.assertIs(raised.exception, self.error)
        self.win_error.assert_called_once_with()
        self.run.assert_not_called()

    def test_sleep_dll_load_failure_propagates_without_subprocess_fallback(self):
        self.load.side_effect = self.error
        with self.assertRaises(OSError) as raised:
            self.module.perform('sleep')
        self.assertIs(raised.exception, self.error)
        self.run.assert_not_called()
        self.suspend.assert_not_called()


if __name__ == '__main__':
    unittest.main()
