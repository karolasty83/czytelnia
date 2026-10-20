"""Explicit Windows power actions. Importing this module performs no action.

The caller is responsible for confirmation, pausing and saving before perform.
Private seams are replaceable by mocks; no subprocess or DLL is used on import.
"""
import ctypes
import ntpath
import os
import subprocess

_run = subprocess.run


def _load_powrprof():
    return ctypes.WinDLL('powrprof.dll', use_last_error=True)


def _win_error():
    return ctypes.WinError(ctypes.get_last_error())


def perform(action):
    """Perform sleep/shutdown; return None, raising on unsupported OS or failure."""
    if type(action) is not str or action not in ('sleep', 'shutdown'):
        raise ValueError('Power action must be sleep or shutdown')
    if os.name != 'nt':
        raise OSError('Power actions require Windows')
    if action == 'sleep':
        suspend = _load_powrprof().SetSuspendState
        # Win32 BOOLEAN is one unsigned byte, unlike the four-byte BOOL.
        suspend.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte]
        suspend.restype = ctypes.c_ubyte
        if not suspend(False, False, False):  # No hibernate, force, or wake disabling.
            raise _win_error()
        return None
    root = os.environ.get('SystemRoot')
    if not root or '\x00' in root or not ntpath.isabs(root) or not ntpath.splitdrive(root)[0]:
        raise OSError('SystemRoot must be an absolute Windows path')
    executable = ntpath.join(root, 'System32', 'shutdown.exe')
    _run([executable, '/s', '/t', '0'], shell=False, check=True, timeout=15,
         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
