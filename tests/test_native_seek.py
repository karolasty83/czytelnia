"""Unit tests; opt-in Windows tests use zero PCM and SDL dummy only."""
import importlib.util
from pathlib import Path
import unittest
import os
import subprocess
import threading
import time
import wave


def load_native():
    native = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(native)
    return native

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_seek_under_test', ROOT / 'addon/globalPlugins/czytelnia/native_seek.py')

class GeometryTests(unittest.TestCase):
    def test_quantized_absolute_target_is_bounded_and_reported(self):
        self.assertTrue(Path(SPEC.origin).is_file(), 'native seek module has not been implemented')
        native = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(native)
        point = native.seek_point(120.456, 180, 24000)
        self.assertEqual(point.x, 16061)
        self.assertAlmostEqual(point.seconds, 120.4575)
        self.assertAlmostEqual(point.max_error, 180 / 48000)
        self.assertLessEqual(abs(point.seconds - 120.456), point.max_error)

    def test_rejects_end_invalid_numbers_and_unaddressable_pixels(self):
        native = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(native)
        for seconds, duration, width in [(180,180,24000), (-1,180,24000), (float('nan'),180,24000), (1,float('inf'),24000), (1,0,24000), (1,180,32768), (1,180,0), (1,180,1.5)]:
            with self.subTest(args=(seconds,duration,width)):
                with self.assertRaises(ValueError):
                    native.seek_point(seconds,duration,width)
        self.assertEqual(native.seek_point(0,180,24000).x, 0)
        # Last partial pixel clamps below EOF; its error bound must be honest.
        p = native.seek_point(179.9999,180,24000)
        self.assertLess(p.x,24000)
        self.assertLessEqual(abs(p.seconds-179.9999),p.max_error)

class ClockTests(unittest.TestCase):
    def test_rejects_invalid_speed_and_malformed_clock_markers(self):
        n = load_native()
        for speed in (0, -1, float('nan'), float('inf'), '2', True):
            with self.subTest(speed=speed):
                with self.assertRaises(ValueError):
                    n.NativeClock(speed)
        c = n.NativeClock(2)
        for record in ('nan M-A: 0', 'inf M-A: 0', '[ashowinfo@native_anchor @ 1] n:0 pts:100 pts_time:1 rate:0', '[ashowinfo@another @ 1] n:0 pts:22050 pts_time:1 rate:22050'):
            self.assertIsNone(c.feed(record))
        self.assertIsNone(c.anchor)
        c.feed('[ashowinfo@native_anchor @ 1] n:0 pts:22050 pts_time:1 rate:22050')
        self.assertIsNone(c.feed('9'*400 + ' M-A: 0'))
        self.assertFalse(c.ready)
        self.assertAlmostEqual(c.feed('1.1 M-A: 0'),1.2)
        self.assertIsNone(c.feed('9'*400 + ' M-A: 0'))

    def test_reanchors_speed_mapping_on_each_filter_reset(self):
        n = load_native()
        self.assertTrue(hasattr(n, 'NativeClock'), 'source clock mapper missing')
        for speed in (1, 1.5, 2):
            c = n.NativeClock(speed)
            self.assertIsNone(c.feed(' 10.3 M-A: 0.000'))
            self.assertIsNone(c.feed('[ashowinfo@native_anchor @ 0001] n:0 pts:220500 pts_time:10 fmt:s16 channels:1 rate:22050'))
            self.assertAlmostEqual(c.feed(' 10.1 M-A: 0.000'), 10 + .1*speed)
            c.begin_seek()
            self.assertIsNone(c.feed(' 10.2 M-A: 0.000'))
            c.feed('[ashowinfo@native_anchor @ 0002] n:0 pts:1325646 pts_time:60.12 fmt:s16 channels:1 rate:22050')
            self.assertIsNone(c.feed(' 10.3 M-A: 0.000'))  # old device-buffer clock
            self.assertAlmostEqual(c.feed(' 60.10 M-A: 0.000'), 60.12)  # latency underrun clamp
            self.assertAlmostEqual(c.feed(' 61.12 M-A: 0.000'), 60.12 + speed)
            c.begin_seek()
            c.feed('[ashowinfo@native_anchor @ 0002] n:0 pts:441000 pts_time:20 fmt:s16 channels:1 rate:22050')
            self.assertIsNone(c.feed(' 61.2 M-A: 0.000'))
            self.assertAlmostEqual(c.feed(' 20.1 M-A: 0.000'),20 + .1*speed)
            # Once the new epoch has begun, clocks need not stay near the anchor.
            self.assertAlmostEqual(c.feed(' 23.0 M-A: 0.000'),20 + 3*speed)

class TerminationRaceTests(unittest.TestCase):
    def make_process(self, polls):
        import io
        from types import SimpleNamespace
        from unittest.mock import Mock
        n = load_native()
        p = n.NativeProcess.__new__(n.NativeProcess)
        p._lock = threading.RLock()
        p._process = 123
        p._desktop = 456
        p.stderr = io.BytesIO()
        p.returncode = None
        p.args = ['fake-ffplay']
        error = PermissionError(5, 'Access is denied')
        def check(result):
            if not result:
                raise error
            return result
        p._api = SimpleNamespace(
            check=check,
            k=SimpleNamespace(TerminateProcess=Mock(return_value=0), CloseHandle=Mock(return_value=1)),
            u=SimpleNamespace(CloseDesktop=Mock(return_value=1)))
        values = iter(polls)
        def poll():
            p.returncode = next(values)
            return p.returncode
        p.poll = Mock(side_effect=poll)
        return p, error

    def test_exit_between_poll_and_terminate_allows_cleanup(self):
        for method in ('terminate', 'kill', 'close'):
            with self.subTest(method=method):
                p, _ = self.make_process([None, 0, 0, 0])
                getattr(p, method)()
                p.close()
                p.close()
                self.assertEqual(p.returncode, 0)
                self.assertTrue(p.stderr.closed)
                self.assertIsNone(p._process)
                self.assertIsNone(p._desktop)
                p._api.k.TerminateProcess.assert_called_once_with(123, 1)
                p._api.k.CloseHandle.assert_called_once_with(123)
                p._api.u.CloseDesktop.assert_called_once_with(456)

    def test_failed_termination_of_live_process_preserves_original_error(self):
        for method in ('terminate', 'kill', 'close'):
            with self.subTest(method=method):
                p, error = self.make_process([None, None, 0, 0])
                with self.assertRaises(PermissionError) as raised:
                    getattr(p, method)()
                self.assertIs(raised.exception, error)
                self.assertEqual(p.poll.call_count, 2)
                p._api.k.TerminateProcess.assert_called_once_with(123, 1)
                p._api.k.CloseHandle.assert_not_called()
                p._api.u.CloseDesktop.assert_not_called()
                self.assertFalse(p.stderr.closed)
                self.assertEqual(p._process, 123)
                self.assertEqual(p._desktop, 456)
                # Ownership survives a genuine failure, so cleanup is retryable.
                p.close()
                self.assertTrue(p.stderr.closed)
                self.assertIsNone(p._process)
                self.assertIsNone(p._desktop)

    def test_failed_exit_check_does_not_suppress_termination_failure(self):
        from unittest.mock import Mock
        p, error = self.make_process([])
        poll_error = OSError(6, 'Cannot query process')
        p.poll = Mock(side_effect=[None, poll_error, 0, 0])
        with self.assertRaises(OSError) as raised:
            p.terminate()
        self.assertIs(raised.exception, poll_error)
        self.assertIs(raised.exception.__context__, error)
        p._api.k.CloseHandle.assert_not_called()
        p.close()
        self.assertTrue(p.stderr.closed)
        self.assertIsNone(p._process)
        self.assertIsNone(p._desktop)

@unittest.skipUnless(os.name == 'nt' and os.environ.get('CZYTELNIA_NATIVE_TEST') == '1', 'opt-in silent Windows native test')
class WindowsTransportTests(unittest.TestCase):
    def test_rejects_conflicting_or_nonlocal_command_before_launch(self):
        n = load_native()
        base = [str(ROOT/'ffplay.exe'),'-vn','-protocol_whitelist','file,pipe','irrelevant.wav']
        from unittest.mock import patch
        with patch.object(n.NativeProcess, '_spawn') as spawn:
            for option in (['-nodisp'], ['-bytes','1'], ['-x','300'], ['-y','200'], ['-fs'], ['-an'], ['-exitonmousedown']):
                with self.subTest(option=option):
                    with self.assertRaises(ValueError):
                        n.NativeProcess(base[:1] + option + base[1:])
            for args in ([], [str(ROOT/'ffplay.exe'),'-vn','irrelevant.wav']):
                with self.assertRaises(ValueError):
                    n.NativeProcess(args)
            spawn.assert_not_called()

    def test_createprocess_failure_does_not_leak_handles(self):
        n = load_native()
        import ctypes as C
        from ctypes import wintypes as W
        k = C.WinDLL('kernel32')
        k.GetCurrentProcess.restype = W.HANDLE
        k.GetProcessHandleCount.argtypes = [W.HANDLE,C.POINTER(W.DWORD)]
        def count():
            value = W.DWORD()
            self.assertTrue(k.GetProcessHandleCount(k.GetCurrentProcess(),C.byref(value)))
            return value.value
        # Win32 lazily opens process-wide GUI/WinStation handles on first use.
        # Warm that subsystem before measuring per-launch ownership.
        with self.assertRaises(OSError):
            n.NativeProcess([str(ROOT/'.test-work/native-seek-build/nonexistent.exe'),'-vn','-protocol_whitelist','file,pipe','none.wav'])
        before = count()
        for _ in range(12):
            with self.assertRaises(OSError):
                n.NativeProcess([str(ROOT/'.test-work/native-seek-build/nonexistent.exe'),'-vn','-protocol_whitelist','file,pipe','none.wav'])
        self.assertEqual(count(), before)

    def test_repeated_natural_exit_kill_and_close_do_not_leak_handles(self):
        n = load_native()
        import ctypes as C
        from ctypes import wintypes as W
        k = C.WinDLL('kernel32')
        k.GetCurrentProcess.restype = W.HANDLE
        k.GetProcessHandleCount.argtypes = [W.HANDLE,C.POINTER(W.DWORD)]
        def count():
            value = W.DWORD();self.assertTrue(k.GetProcessHandleCount(k.GetCurrentProcess(),C.byref(value)))
            return value.value
        out = ROOT/'.test-work/native-seek-build'
        out.mkdir(parents=True,exist_ok=True)
        wav = out/'short-zero.wav'
        with wave.open(str(wav),'wb') as f:
            f.setparams((1,2,22050,0,'NONE','not compressed'));f.writeframes(bytes(44100))
        args = [str(ROOT/'ffplay.exe'),'-hide_banner','-vn','-autoexit','-volume','0','-protocol_whitelist','file,pipe',str(wav)]
        env = dict(os.environ,SDL_AUDIODRIVER='dummy',SDL_VIDEODRIVER='windows',SDL_RENDER_DRIVER='software')
        observations=[]
        for mode in ('warm','natural','kill','close'):
            p=n.NativeProcess(args,env=env)
            data=bytearray()
            def drain():
                while chunk:=p.stderr.read1(4096):data.extend(chunk)
            thread=threading.Thread(target=drain);thread.start()
            try:
                if mode=='kill':p.kill()
                if mode=='close':p.close()
                code=p.wait(timeout=5)
                if mode in ('warm','natural'):self.assertEqual(code,0)
            finally:
                p.close();thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(p.stderr.closed)
            self.assertIsNone(p._process)
            self.assertIsNone(p._desktop)
            observations.append(count())
        self.assertEqual(observations,[observations[0]]*len(observations))
        import json
        (out/'cleanup-unit.json').write_text(json.dumps({'modes':['warm','natural','kill','close'],'handles_after':observations}))

    def test_natural_exit_between_poll_and_terminate_closes_real_resources(self):
        import ctypes as C
        import json
        n = load_native()
        api = n._WindowsAPI()
        api.k.GetCurrentProcess.restype = api.W.HANDLE
        api.k.GetProcessHandleCount.argtypes = [api.W.HANDLE, C.POINTER(api.W.DWORD)]
        api.u.GetForegroundWindow.restype = api.W.HWND
        foreground = api.u.GetForegroundWindow()
        def count():
            value = api.W.DWORD()
            api.check(api.k.GetProcessHandleCount(api.k.GetCurrentProcess(), C.byref(value)))
            return value.value
        out = ROOT / '.test-work/native-seek-build'
        out.mkdir(parents=True, exist_ok=True)
        wav = out / 'cleanup-race.wav'
        with wave.open(str(wav), 'wb') as f:
            f.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
            f.writeframes(bytes(44100))
        args = [str(ROOT/'ffplay.exe'), '-hide_banner', '-vn', '-autoexit', '-volume', '0',
                '-protocol_whitelist', 'file,pipe', str(wav)]
        env = dict(os.environ, SDL_AUDIODRIVER='dummy', SDL_VIDEODRIVER='windows', SDL_RENDER_DRIVER='software')
        observations = []
        for method in ('close', 'terminate', 'kill', 'close'):
            p = n.NativeProcess(args, env=env)
            reader = threading.Thread(target=lambda: p.stderr.read())
            reader.start()
            real_poll = p.poll
            real_check = p._api.check
            errors = []
            raced = False
            def delayed_poll():
                nonlocal raced
                if not raced:
                    raced = True
                    self.assertIsNone(real_poll())
                    # Hold the stale live result while ffplay exits naturally.
                    # TerminateProcess itself remains the actual Win32 call.
                    deadline = time.monotonic() + 5
                    while real_poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(real_poll(), 0)
                    return None
                return real_poll()
            def checked(result):
                try:
                    return real_check(result)
                except OSError as error:
                    errors.append(error.winerror)
                    raise
            p.poll = delayed_poll
            p._api.check = checked
            try:
                getattr(p, method)()
                p.close()
                p.close()
                reader.join(5)
                self.assertEqual(errors, [5])
                self.assertTrue(raced)
                self.assertFalse(reader.is_alive())
                self.assertTrue(p.stderr.closed)
                self.assertIsNone(p._process)
                self.assertIsNone(p._desktop)
                self.assertEqual(p.poll(), 0)
                self.assertEqual(api.u.GetForegroundWindow(), foreground)
                observations.append({'method': method, 'winerrors': errors, 'handles_after': count()})
            finally:
                p.poll = real_poll
                p._api.check = real_check
                p.close()
                reader.join(5)
        self.assertEqual([item['handles_after'] for item in observations],
                         [observations[0]['handles_after']] * len(observations))
        (out/'cleanup-race.json').write_text(json.dumps(observations), encoding='utf-8')

    def test_private_native_process_pipe_seek_and_explicit_cleanup(self):
        n = load_native()
        self.assertTrue(hasattr(n, 'NativeProcess'), 'native Popen-like transport missing')
        import ctypes as C
        from ctypes import wintypes as W
        u = C.WinDLL('user32')
        u.GetForegroundWindow.restype = W.HWND
        foreground = u.GetForegroundWindow()
        out = ROOT / '.test-work/native-seek-build'
        out.mkdir(exist_ok=True, parents=True)
        wav = out / 'transport-zero.wav'
        with wave.open(str(wav), 'wb') as f:
            f.setparams((1,2,22050,0,'NONE','not compressed'))
            for _ in range(180):
                f.writeframesraw(bytes(44100))
        args = [str(ROOT/'ffplay.exe'),'-hide_banner','-vn','-stats','-autoexit','-volume','0','-ss','10','-af','ashowinfo@native_anchor,atempo=1','-protocol_whitelist','file,pipe',str(wav)]
        env = dict(os.environ, SDL_AUDIODRIVER='dummy', SDL_RENDER_DRIVER='software', SDL_VIDEODRIVER='windows')
        self.assertEqual(args[args.index('-volume')+1], '0')
        p = n.NativeProcess(args, env=env)
        data = bytearray()
        def drain():
            while block := p.stderr.read1(4096):
                data.extend(block)
        reader = threading.Thread(target=drain)
        reader.start()
        try:
            self.assertIsNone(p.poll())
            with self.assertRaises(subprocess.TimeoutExpired):
                p.wait(timeout=.01)
            deadline = time.monotonic()+5
            while b'pts_time:10' not in data and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertIn(b'pts_time:10',data)
            pid = p.pid
            point = p.seek(60.123,180)
            deadline = time.monotonic()+3
            while b'pts_time:60.' not in data and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertIn(b'pts_time:60.',data)
            self.assertLess(abs(point.seconds - 60.123), .01)
            self.assertEqual(p.pid,pid)
            self.assertIsNone(p.poll())
        finally:
            p.terminate()
            p.wait(timeout=5)
            reader.join(5)
            p.close()
            p.close()
            (out/'transport-unit.log').write_bytes(data)
        self.assertFalse(reader.is_alive())
        self.assertTrue(p.stderr.closed)
        self.assertEqual(u.GetForegroundWindow(),foreground)
        self.assertIsNotNone(p.poll())
        with self.assertRaises(RuntimeError):
            p.seek(30,180)

if __name__ == '__main__':
    unittest.main()
