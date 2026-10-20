"""Isolated Windows ffplay seek transport. Not enabled by Player automatically."""
from collections import namedtuple
import math
import re


class NativeClock:
    """Map ffplay OUTPUT time using actual first source PTS after every seek.

    Put ``ashowinfo@native_anchor`` BEFORE atempo and keep loglevel info. Feed
    complete CR/LF-separated records in stderr order. Call begin_seek under the
    owner's callback/reader lock before posting, and serialize seeks until a new
    clock is accepted. This object is not thread-safe. A fresh anchor is decode
    evidence, not an audible-transition ACK. Old clocks far from the anchor are
    ignored until one falls in its startup window; very small seeks whose old
    clock overlaps that window cannot be distinguished from stats alone.
    """
    _anchor = re.compile(r'\[ashowinfo@native_anchor\s+@[^\]]+\]\s+n:0\s+pts:(-?\d+)\s+.*?\brate:(\d+)')
    _clock = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s+M-A:')

    def __init__(self, speed):
        if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
            raise ValueError('Speed must be finite and positive')
        self.speed = speed
        self.anchor = None
        self.ready = False

    def begin_seek(self):
        self.anchor = None
        self.ready = False

    def feed(self, record):
        marker = self._anchor.search(record)
        if marker:
            try:
                anchor = int(marker[1]) / int(marker[2])
            except (ValueError, OverflowError, ZeroDivisionError):
                return None
            if not math.isfinite(anchor):
                return None
            self.anchor = anchor
            self.ready = False
            return None
        clock = self._clock.match(record)
        if clock is None or self.anchor is None:
            return None
        raw = float(clock[1])
        if not math.isfinite(raw):
            return None
        if not self.ready and not self.anchor - .5 <= raw <= self.anchor + .5:
            return None
        self.ready = True
        return self.anchor + max(0.0, raw - self.anchor) * self.speed

SeekPoint = namedtuple('SeekPoint', 'x seconds max_error')


def seek_point(seconds, duration, width):
    """Quantize a target; bound is ideal geometry, not decoder/SDL-DPI rounding.

    End-of-track is deliberately rejected: the owner must stop/cancel without
    dispatching natural EOF. Near-end quantization clamps one pixel below EOF.
    WM_MOUSEMOVE coordinates are signed 16-bit on Windows.
    """
    if (type(seconds) not in (int, float) or type(duration) not in (int, float)
            or not math.isfinite(seconds) or not math.isfinite(duration)
            or not 0 <= seconds < duration
            or type(width) is not int or not 1 <= width <= 32767):
        raise ValueError('Seek requires 0 <= target < finite duration and width 1..32767')
    x = min(round(seconds / duration * width), width - 1)
    value = x / width * duration
    return SeekPoint(x, value, max(duration / (2 * width), abs(value - seconds)))


class NativeProcess:
    """Small Popen-like Windows process with an explicit private desktop.

    Caller supplies the normal ffplay argv, WITHOUT -nodisp. This transport adds
    a wide, low private window and forces time (not byte) seeking. The window is
    never on the input desktop. stdout/stdin are NUL; stderr is a binary buffered
    pipe with read1. Drain stderr concurrently. Call close after the reader exits
    to release process/desktop/pipe handles (also required after natural EOF).

    seek returns a SeekPoint after POSTING; it is not an ACK. Serialize seeks and
    wait for a new source marker + valid clock, with a bounded timeout, before
    posting another. ffplay silently coalesces seeks while seek_req is set.
    No pause/resume semantics or application callbacks are implemented here.
    """

    def __init__(self, args, *, env=None, cwd=None):
        import os
        import threading
        import subprocess
        if os.name != 'nt':
            raise OSError('Native ffplay seeking requires Windows')
        self._lock = threading.RLock()
        self.returncode = None
        self._process = None
        self._desktop = None
        self.stderr = None
        self._api = _WindowsAPI()
        self.args = [str(arg) for arg in args]
        forbidden = {'-nodisp', '-bytes', '-x', '-y', '-fs', '-an', '-exitonmousedown', '-exitonkeydown'}
        if not self.args or forbidden.intersection(self.args):
            raise ValueError('Missing argv or conflicting ffplay window/seek options')
        if '-vn' not in self.args or self.args.count('-protocol_whitelist') != 1:
            raise ValueError('Require audio-only -vn and local protocol whitelist')
        index = self.args.index('-protocol_whitelist')
        if index + 2 >= len(self.args) or self.args[index + 1] != 'file,pipe':
            raise ValueError('Require -protocol_whitelist file,pipe before the input')
        self.args[1:1] = ['-bytes', '0', '-x', '30000', '-y', '32', '-noborder']
        self._spawn(env, cwd)

    def _spawn(self, env, cwd):
        import ctypes as C
        import io
        import msvcrt
        import os
        import subprocess
        import uuid
        a = self._api
        self.desktop_name = 'CzytelniaPrivate_' + uuid.uuid4().hex
        read_fd = write_fd = null_fd = None
        attributes = None
        pi = a.PI()
        try:
            self._desktop = a.u.CreateDesktopW(self.desktop_name, None, None, 0, 0x1ff, None)
            a.check(self._desktop)
            read_fd, write_fd = os.pipe()
            null_fd = os.open(os.devnull, os.O_RDWR | os.O_BINARY)
            handles = (a.W.HANDLE * 2)(msvcrt.get_osfhandle(write_fd), msvcrt.get_osfhandle(null_fd))
            for h in handles:
                os.set_handle_inheritable(h, True)
            size = C.c_size_t()
            a.k.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
            storage = C.create_string_buffer(size.value)
            a.check(a.k.InitializeProcThreadAttributeList(storage, 1, 0, C.byref(size)))
            attributes = storage
            a.check(a.k.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, C.sizeof(handles), None, None))
            si = a.SIEX()
            si.StartupInfo.cb = C.sizeof(si)
            si.StartupInfo.lpDesktop = 'winsta0\\' + self.desktop_name
            si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput = handles[1]
            si.StartupInfo.hStdOutput = handles[1]
            si.StartupInfo.hStdError = handles[0]
            si.lpAttributeList = C.cast(attributes, C.c_void_p)
            environment = dict(os.environ if env is None else env)
            block = C.create_unicode_buffer('\0'.join(f'{key}={value}' for key, value in sorted(environment.items(), key=lambda kv: kv[0].upper())) + '\0\0')
            cmd = C.create_unicode_buffer(subprocess.list2cmdline(self.args))
            flags = 0x08000000 | 0x400 | 0x80000  # NO_WINDOW, UNICODE_ENV, EXTENDED_STARTUPINFO
            a.check(a.k.CreateProcessW(None, cmd, None, None, True, flags, block,
                                      None if cwd is None else str(cwd), C.byref(si), C.byref(pi)))
            self._process = pi.hProcess
            self.pid = pi.dwProcessId
            self.stderr = io.BufferedReader(os.fdopen(read_fd, 'rb', buffering=0))
            read_fd = None
        except BaseException:
            if self._process:
                a.k.TerminateProcess(self._process, 1)
                a.k.WaitForSingleObject(self._process, 5000)
                a.k.CloseHandle(self._process)
                self._process = None
            if self._desktop:
                a.u.CloseDesktop(self._desktop)
                self._desktop = None
            raise
        finally:
            if pi.hThread:
                a.k.CloseHandle(pi.hThread)
            if attributes is not None:
                a.k.DeleteProcThreadAttributeList(attributes)
            for fd in (read_fd, write_fd, null_fd):
                if fd is not None:
                    os.close(fd)

    def poll(self):
        import ctypes as C
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            if not self._process:
                raise RuntimeError('Process handle is closed')
            result = self._api.k.WaitForSingleObject(self._process, 0)
            if result == 258:
                return None
            if result != 0:
                raise C.WinError(C.get_last_error())
            code = self._api.W.DWORD()
            self._api.check(self._api.k.GetExitCodeProcess(self._process, C.byref(code)))
            self.returncode = code.value
            return self.returncode

    def wait(self, timeout=None):
        import time
        import subprocess
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.01)

    def terminate(self):
        with self._lock:
            if self.poll() is None:
                try:
                    self._api.check(self._api.k.TerminateProcess(self._process, 1))
                except OSError:
                    # ffplay may exit naturally after the initial poll. Only
                    # ignore the failed termination when exit is confirmed.
                    if self.poll() is None:
                        raise

    kill = terminate

    def _window(self, timeout):
        import ctypes as C
        import time
        a = self._api
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self.poll() is not None or not self._desktop:
                    raise RuntimeError('Cannot seek an exited process')
                found = []
                @a.CALLBACK
                def callback(hwnd, param):
                    pid = a.W.DWORD()
                    a.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
                    if pid.value == self.pid:
                        name = C.create_unicode_buffer(256)
                        a.u.GetClassNameW(hwnd, name, 256)
                        if name.value == 'SDL_app':
                            found.append(hwnd)
                    return True
                a.check(a.u.EnumDesktopWindows(self._desktop, callback, 0))
                if found:
                    return found[0]
            if time.monotonic() >= deadline:
                raise TimeoutError('No SDL window on private desktop')
            time.sleep(.01)

    def seek(self, seconds, duration, *, timeout=2.0):
        import ctypes as C
        hwnd = self._window(timeout)
        a = self._api
        with self._lock:
            if self.poll() is not None:
                raise RuntimeError('Cannot seek an exited process')
            rect = a.W.RECT()
            a.check(a.u.GetClientRect(hwnd, C.byref(rect)))
            point = seek_point(seconds, duration, rect.right - rect.left)
            position = point.x | (1 << 16)
            # SDL uses the last mouse-motion coordinates for button presses.
            for message, flags in ((0x200, 0), (0x204, 2), (0x205, 0)):
                a.check(a.u.PostMessageW(hwnd, message, flags, position))
            return point

    def close(self):
        """Reap before closing pipe; idempotent, including after natural exit."""
        if self._process:
            self.terminate()
            self.wait(timeout=5)
        with self._lock:
            if self.stderr is not None and not self.stderr.closed:
                self.stderr.close()
            if self._process:
                self._api.check(self._api.k.CloseHandle(self._process))
                self._process = None
            if self._desktop:
                self._api.check(self._api.u.CloseDesktop(self._desktop))
                self._desktop = None


class _WindowsAPI:
    """Per-instance Win32 bindings; no subprocess/global registry mutation."""
    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W
        self.W = W
        self.u = C.WinDLL('user32', use_last_error=True)
        self.k = C.WinDLL('kernel32', use_last_error=True)
        class SI(C.Structure):
            _fields_ = [('cb',W.DWORD),('lpReserved',W.LPWSTR),('lpDesktop',W.LPWSTR),('lpTitle',W.LPWSTR),
                ('dwX',W.DWORD),('dwY',W.DWORD),('dwXSize',W.DWORD),('dwYSize',W.DWORD),('dwXCountChars',W.DWORD),('dwYCountChars',W.DWORD),
                ('dwFillAttribute',W.DWORD),('dwFlags',W.DWORD),('wShowWindow',W.WORD),('cbReserved2',W.WORD),('lpReserved2',C.c_void_p),
                ('hStdInput',W.HANDLE),('hStdOutput',W.HANDLE),('hStdError',W.HANDLE)]
        class SIEX(C.Structure):
            _fields_ = [('StartupInfo',SI),('lpAttributeList',C.c_void_p)]
        class PI(C.Structure):
            _fields_ = [('hProcess',W.HANDLE),('hThread',W.HANDLE),('dwProcessId',W.DWORD),('dwThreadId',W.DWORD)]
        self.SIEX, self.PI = SIEX, PI
        self.CALLBACK = C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        bindings = [
            (self.u.CreateDesktopW,[W.LPCWSTR,W.LPCWSTR,C.c_void_p,W.DWORD,W.DWORD,C.c_void_p],W.HANDLE),
            (self.u.CloseDesktop,[W.HANDLE],W.BOOL),
            (self.u.EnumDesktopWindows,[W.HANDLE,self.CALLBACK,W.LPARAM],W.BOOL),
            (self.u.GetWindowThreadProcessId,[W.HWND,C.POINTER(W.DWORD)],W.DWORD),
            (self.u.GetClassNameW,[W.HWND,W.LPWSTR,C.c_int],C.c_int),
            (self.u.GetClientRect,[W.HWND,C.POINTER(W.RECT)],W.BOOL),
            (self.u.PostMessageW,[W.HWND,W.UINT,W.WPARAM,W.LPARAM],W.BOOL),
            (self.k.CreateProcessW,[W.LPCWSTR,W.LPWSTR,C.c_void_p,C.c_void_p,W.BOOL,W.DWORD,C.c_void_p,W.LPCWSTR,C.POINTER(SIEX),C.POINTER(PI)],W.BOOL),
            (self.k.GetExitCodeProcess,[W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
            (self.k.TerminateProcess,[W.HANDLE,W.UINT],W.BOOL),
            (self.k.WaitForSingleObject,[W.HANDLE,W.DWORD],W.DWORD),
            (self.k.CloseHandle,[W.HANDLE],W.BOOL),
            (self.k.InitializeProcThreadAttributeList,[C.c_void_p,W.DWORD,W.DWORD,C.POINTER(C.c_size_t)],W.BOOL),
            (self.k.UpdateProcThreadAttribute,[C.c_void_p,W.DWORD,C.c_size_t,C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p],W.BOOL),
            (self.k.DeleteProcThreadAttributeList,[C.c_void_p],None),
        ]
        for function, arguments, result in bindings:
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def check(result):
        if not result:
            import ctypes as C
            raise C.WinError(C.get_last_error())
        return result
