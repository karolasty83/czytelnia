"""Standalone backend tests: no NVDA runtime and no third-party packages."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock
import subprocess
import tempfile
import json
import threading
import time
import queue

WORK = Path(__file__).resolve().parents[1] / '.test-work' / 'player'
WORK.mkdir(parents=True, exist_ok=True)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "standalone_player", ROOT / "addon/globalPlugins/czytelnia/player.py")
player = importlib.util.module_from_spec(SPEC)
if Path(SPEC.origin).exists():
    SPEC.loader.exec_module(player)


class ClockTests(unittest.TestCase):
    def test_real_ffplay_8_audio_clock_is_absolute_after_ss(self):
        self.assertTrue(hasattr(player, "parse_clock"), "audio clock parser missing")
        # Captured from the supplied ffplay.exe v8 with -ss 3 on an 8-second WAV.
        samples = [
            ("    nan    :  0.000 fd=   0 aq=    0KB vq=    0KB sq=    0B ", None),
            ("    nan M-A:    nan fd=   0 aq=   52KB vq=    0KB sq=    0B ", None),
            ("   2.95 M-A: -0.000 fd=   0 aq=   52KB vq=    0KB sq=    0B ", 2.95),
            ("   7.94 M-A:  0.000 fd=   0 aq=    0KB vq=    0KB sq=    0B ", 7.94),
            ("  Duration: 00:00:08.00, bitrate: 256 kb/s", None),
            ("   0.25 A-V:  0.000 fd=   0", None),
        ]
        for line, expected in samples:
            with self.subTest(line=line):
                self.assertEqual(player.parse_clock(line), expected)


class SettingTests(unittest.TestCase):
    def test_speed_accepts_only_finite_half_steps_and_survives_load(self):
        instance = player.Player('ffplay.exe', 'ffprobe.exe')
        self.assertTrue(hasattr(instance, 'speed'), 'speed property missing')
        self.assertEqual(instance.speed, 1.0)
        for value in (0.5, 1, 1.5, 2):
            instance.set_speed(value)
            self.assertEqual(instance.speed, value)
        for value in (0, -1, 0.49, 2.01, 2.5, 3, 3.01, 4, 1.25, 1.50000001, '2', None, True,
                      float('nan'), float('inf'), float('-inf')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    instance.set_speed(value)
                self.assertEqual(instance.speed, 2)
        result = subprocess.CompletedProcess([], 0, '{"format":{"duration":"8"}}', '')
        with mock.patch.object(player.subprocess, 'run', return_value=result):
            instance.load(__file__, position=3)
            self.assertEqual(instance.speed, 2)
            self.assertEqual(instance.duration, 8)
            self.assertEqual(instance.position, 3)
            self.assertFalse(instance.is_playing)
            instance.load(__file__, position=1)
            self.assertEqual(instance.speed, 2, 'new part must preserve book speed')
        instance.close()
        with self.assertRaises(RuntimeError):
            instance.set_speed(1)

    def test_volume_is_validated_integer_and_readable_without_loading(self):
        instance = player.Player('ffplay.exe', 'ffprobe.exe')
        self.assertTrue(hasattr(instance, 'volume'), 'volume property missing')
        self.assertEqual(instance.volume, 100)
        for value in (0, 1, 50, 100):
            instance.set_volume(value)
            self.assertEqual(instance.volume, value)
        for value in (-1, 101, 0.5, 50.0, '50', None, True, float('nan'), float('inf')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    instance.set_volume(value)
                self.assertEqual(instance.volume, 100)
                with self.assertRaises(ValueError):
                    player.Player('ffplay.exe', 'ffprobe.exe', volume=value)
        instance.close()
        with self.assertRaises(RuntimeError):
            instance.set_volume(0)


class LoadTests(unittest.TestCase):
    def test_known_duration_skips_probe_and_clamps_position(self):
        self.assertIn('duration', __import__('inspect').signature(player.Player.load).parameters)
        instance = player.Player('ffplay.exe', 'ffprobe.exe')
        self.addCleanup(instance.close)
        with mock.patch.object(player.subprocess, 'run', side_effect=AssertionError('unneeded probe')):
            instance.load(__file__, position=99, duration=12.5)
        self.assertEqual(instance.duration, 12.5)
        self.assertEqual(instance.position, 12.5)
        self.assertFalse(instance.is_playing)

    def test_known_duration_invalid_values_do_not_probe_or_change_player(self):
        self.assertIn('duration', __import__('inspect').signature(player.Player.load).parameters)
        instance = player.Player('ffplay.exe', 'ffprobe.exe')
        self.addCleanup(instance.close)
        with mock.patch.object(player.subprocess, 'run', side_effect=AssertionError('unneeded probe')):
            instance.load(__file__, position=2, duration=10)
            for value in (0, -1, True, '12', float('nan'), float('inf'), 10 ** 1000):
                with self.subTest(value=str(value)[:30]):
                    with self.assertRaises(ValueError):
                        instance.load(__file__, duration=value)
                    self.assertEqual((instance.duration, instance.position), (10, 2))

    def test_import_probe_helper_keeps_player_probe_security_contract(self):
        self.assertTrue(hasattr(player, 'probe_duration'))
        result = subprocess.CompletedProcess([], 0, '{"format":{"duration":"12.50"}}', '')
        with mock.patch.object(player.subprocess, 'run', return_value=result) as run:
            self.assertEqual(player.probe_duration(__file__, 'bundled-ffprobe'), 12.5)
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'bundled-ffprobe')
        self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file,pipe')
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        self.assertFalse(run.call_args.kwargs['shell'])

    def test_probe_helper_rejects_boolean_json_duration(self):
        result = subprocess.CompletedProcess([], 0, '{"format":{"duration":true}}', '')
        with mock.patch.object(player.subprocess, 'run', return_value=result):
            with self.assertRaises(ValueError):
                player.probe_duration(__file__, 'bundled-ffprobe')

    def test_invalid_probe_and_nonfinite_positions_leave_state_unchanged(self):
        instance = player.Player("ffplay.exe", "ffprobe.exe")
        with tempfile.TemporaryDirectory(dir=WORK) as directory:
            path = Path(directory) / "chapter.wav"
            path.touch()
            for value in ('NaN', 'Infinity', '-1', '0', 'N/A'):
                result = subprocess.CompletedProcess([], 0, json.dumps({"format": {"duration": value}}), '')
                with self.subTest(duration=value), mock.patch.object(player.subprocess, "run", return_value=result):
                    with self.assertRaises(ValueError):
                        instance.load(path)
                    self.assertEqual(instance.duration, 0)
            result = subprocess.CompletedProcess([], 0, '{"format":{"duration":"8"}}', '')
            with mock.patch.object(player.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError):
                    instance.load(path, float('nan'))
            with mock.patch.object(player.subprocess, "run") as run:
                with self.assertRaises(FileNotFoundError):
                    instance.load(Path(directory) / "missing.wav")
                run.assert_not_called()

    def test_probe_timeout_propagates_without_changing_state(self):
        instance = player.Player("ffplay.exe", "ffprobe.exe")
        with mock.patch.object(player.subprocess, "run", side_effect=subprocess.TimeoutExpired('ffprobe', 10)):
            with self.assertRaises(subprocess.TimeoutExpired):
                instance.load(__file__)
        self.assertEqual(instance.duration, 0)

    def test_load_probes_duration_without_shell_with_bounded_timeout(self):
        self.assertTrue(hasattr(player, "Player"), "Player missing")
        result = subprocess.CompletedProcess([], 0, '{"format":{"duration":"12.50"}}', '')
        with tempfile.TemporaryDirectory(dir=WORK) as directory:
            path = Path(directory) / "chapter & próba.wav"
            path.touch()
            with mock.patch.object(player.subprocess, "run", return_value=result) as run:
                instance = player.Player("ffplay.exe", "ffprobe.exe")
                instance.load(path, position=4)
            self.assertEqual(instance.duration, 12.5)
            self.assertEqual(instance.position, 4)
            self.assertFalse(instance.is_playing)
            args, kwargs = run.call_args
            self.assertEqual(args[0][0], "ffprobe.exe")
            self.assertEqual(args[0][-1], str(path.resolve()))
            self.assertIn("json", args[0])
            self.assertIn("-protocol_whitelist", args[0])
            whitelist = args[0].index("-protocol_whitelist")
            self.assertEqual(args[0][whitelist + 1], "file,pipe")
            self.assertLess(whitelist + 1, len(args[0]) - 1)
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 15)
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))


class FakePipe:
    def __init__(self):
        self.chunks = queue.Queue()
        self.closed = False

    def read1(self, size):
        return self.chunks.get(timeout=5)

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.stderr = FakePipe()
        self.returncode = None
        self.done = threading.Event()
        self.terminated = False
        self.killed = False
        self.waits = []

    def emit(self, data):
        self.stderr.chunks.put(data)

    def finish(self, code=0):
        self.returncode = code
        self.done.set()
        self.emit(b'')

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('ffplay', timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.finish(-15)

    def kill(self):
        self.killed = True
        self.finish(-9)


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('condition did not become true')


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.instance = player.Player('ffplay.exe', 'ffprobe.exe',
            lambda event, detail: self.events.append((event, detail, threading.get_ident())),
            volume=0, use_native=False)  # These fixtures emulate subprocess pipes, not Win32.
        self.processes = []
        def spawn(*args, **kwargs):
            process = FakeProcess()
            self.processes.append(process)
            return process
        self.spawn = mock.patch.object(player.subprocess, 'Popen', side_effect=spawn).start()
        self.addCleanup(mock.patch.stopall)
        self.control_send = mock.patch.object(player.VolumeControl, 'set_volume', return_value='0 Success').start()
        result = subprocess.CompletedProcess([], 0, '{"format":{"duration":"8"}}', '')
        with mock.patch.object(player.subprocess, 'run', return_value=result):
            self.instance.load(__file__, position=3)

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.finish(-9)
        if hasattr(self.instance, 'close'):
            self.instance.close()

    def test_volume_switch_preserves_process_generation_and_clock(self):
        self.instance.play()
        process = self.processes[-1]
        process.emit(b'   4.25 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 4.25)
        generation = self.instance._generation
        self.instance.set_volume(75)
        self.assertEqual(self.spawn.call_count, 1, 'volume must not relaunch')
        self.assertIs(self.instance._process, process)
        self.assertFalse(process.terminated)
        self.assertEqual(self.instance._generation, generation)
        self.assertEqual(self.instance.position, 4.25)
        self.assertEqual(self.instance.volume, 75)
        process.emit(b'   4.50 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 4.5)
        self.assertEqual(self.events, [])
        self.instance.set_volume(75)
        with self.assertRaises(ValueError):
            self.instance.set_volume(101)
        self.assertEqual(self.spawn.call_count, 1)
        self.instance.pause()
        self.instance.set_volume(50)
        self.assertFalse(self.instance.is_playing)
        self.assertEqual(self.instance.position, 4.5)
        self.assertEqual(self.spawn.call_count, 1)
        self.instance.seek(8)
        self.instance.set_volume(25)
        self.assertEqual(self.instance.position, 8, 'setting at EOF must not restart')

    def test_live_volume_error_does_not_persist_setting_or_restart(self):
        self.instance.play()
        process = self.instance._process
        generation = self.instance._generation
        self.control_send.side_effect = player.ControlError('ack timeout')
        with self.assertRaisesRegex(player.ControlError, 'timeout'):
            self.instance.set_volume(37)
        self.assertEqual(self.instance.volume, 0)
        self.assertIs(self.instance._process, process)
        self.assertEqual(self.instance._generation, generation)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertFalse(process.terminated)
        self.assertEqual(self.events, [])

    def test_clean_eof_before_startup_ack_keeps_watcher_event(self):
        for speed in (1, 2):
            with self.subTest(speed=speed):
                self.instance.pause()
                self.instance.seek(7.5)
                self.instance.set_speed(speed)
                self.events.clear()
                notify_entered = threading.Event()
                original_notify = self.instance._notify

                def notify(*args):
                    notify_entered.set()
                    return original_notify(*args)

                def fail_ack(*args, **kwargs):
                    process = self.processes[-1]
                    process.emit(b'   7.60 M-A: 0.000 fd= 0\r')
                    process.finish(0)
                    self.assertTrue(notify_entered.wait(1), 'watcher never reached dispatch')
                    raise player.ControlError('ffplay exited before startup ack')

                self.control_send.side_effect = fail_ack
                with mock.patch.object(self.instance, '_notify', side_effect=notify):
                    self.instance.play()
                    eventually(lambda: bool(self.events))
                    eventually(lambda: not self.instance._workers)
                self.assertEqual([e[:2] for e in self.events], [('ended', None)])
                self.assertEqual(self.instance.position, self.instance.duration)
                self.assertFalse(self.instance.is_playing)
                self.assertIsNone(self.instance._volume_control)
                self.assertFalse(self.processes[-1].terminated)
                self.assertNotEqual(self.events[0][2], threading.get_ident())

    def test_startup_eof_waits_for_delayed_stderr_evidence_not_worker_exit(self):
        release_drain, ack_failed, returned = (threading.Event() for _ in range(3))
        original_read = FakePipe.read1
        errors = []

        def delayed_read(pipe, size):
            release_drain.wait(2)
            return original_read(pipe, size)

        def fail_ack(*args, **kwargs):
            process = self.processes[-1]
            process.emit(b'   7.90 M-A: 0.000 fd= 0\r')
            process.finish(0)
            ack_failed.set()
            raise player.ControlError('exit before ack')

        def play():
            try:
                self.instance.play()
            except BaseException as error:
                errors.append(error)
            finally:
                returned.set()

        self.control_send.side_effect = fail_ack
        with mock.patch.object(FakePipe, 'read1', delayed_read):
            caller = threading.Thread(target=play, daemon=True)
            caller.start()
            try:
                self.assertTrue(ack_failed.wait(1))
                self.assertEqual(self.processes[-1].poll(), 0)
                self.assertFalse(returned.wait(.05), 'must await unread clock evidence')
                release_drain.set()
                self.assertTrue(returned.wait(1), 'must not join a dispatch-blocked watcher')
                caller.join(1)
                eventually(lambda: not self.instance._workers)
            finally:
                release_drain.set()
                caller.join(3)
        self.assertEqual(errors, [])
        self.assertEqual([e[:2] for e in self.events], [('ended', None)])
        self.assertEqual(self.instance.position, self.instance.duration)

    def test_startup_drain_timeout_fails_closed_despite_zero_exit(self):
        release_drain = threading.Event()
        original_read = FakePipe.read1

        def delayed_read(pipe, size):
            release_drain.wait(2)
            return original_read(pipe, size)

        def fail_ack(*args, **kwargs):
            process = self.processes[-1]
            process.emit(b'   7.90 M-A: 0.000 fd= 0\r')
            process.finish(0)
            raise player.ControlError('no drained evidence')

        self.control_send.side_effect = fail_ack
        with mock.patch.object(FakePipe, 'read1', delayed_read), \
                mock.patch.object(player, 'PROCESS_TIMEOUT', .02):
            try:
                started = time.monotonic()
                self.instance.play()
                self.assertLess(time.monotonic() - started, .5)
                eventually(lambda: bool(self.events))
                self.assertEqual(self.events[0][0], 'error')
                self.assertEqual(self.instance.position, 3)
            finally:
                release_drain.set()
                eventually(lambda: not self.instance._workers)
        self.assertEqual(len(self.events), 1, 'late drained EOF must stay stale')
        self.assertEqual(self.instance.position, 3)
        self.assertIsNone(self.instance._volume_control)

    def test_startup_exit_requires_clock_and_success_not_just_return_zero(self):
        cases = [(0, b'azmq bind failed: Address already in use\n'),
                 (0, b'Failed to configure filtergraph\n'),
                 (0, b'Invalid data found when processing input\n'),
                 (1, b'   7.90 M-A: 0.000 fd= 0\rdecoder failed\n')]
        for code, diagnostic in cases:
            with self.subTest(code=code, diagnostic=diagnostic):
                self.events.clear()
                self.instance.seek(3)

                def fail_ack(*args, **kwargs):
                    process = self.processes[-1]
                    process.emit(diagnostic)
                    process.finish(code)
                    raise player.ControlError('startup failed')

                self.control_send.side_effect = fail_ack
                self.instance.play()
                eventually(lambda: not self.instance._workers)
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.events[0][0], 'error')
                self.assertIn(diagnostic.decode().strip(), self.events[0][1])
                self.assertLess(self.instance.position, self.instance.duration)
                self.assertFalse(self.instance.is_playing)
                self.assertIsNone(self.instance._volume_control)

    def test_startup_control_failure_closes_process_and_reports_once(self):
        self.control_send.side_effect = player.ControlError('wrong random target')
        self.instance.play()
        eventually(lambda: bool(self.events))
        self.assertFalse(self.instance.is_playing)
        self.assertTrue(self.processes[-1].terminated)
        self.assertIsNone(self.instance._volume_control)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][0], 'error')
        self.assertIn('wrong random target', self.events[0][1])

    def test_startup_failure_keeps_ffplay_diagnostic(self):
        def fail(*args, **kwargs):
            process = self.processes[-1]
            process.emit(b'azmq bind failed: Address already in use\n')
            eventually(lambda: process.stderr.chunks.empty())
            raise player.ControlError('endpoint failed')
        self.control_send.side_effect = fail
        self.instance.play()
        eventually(lambda: bool(self.events))
        self.assertIn('Address already in use', self.events[0][1])

    def test_each_process_has_new_control_and_initial_saved_gain(self):
        self.instance.play()
        old = self.instance._volume_control
        self.instance.pause()
        self.assertTrue(old._closed)
        self.instance.set_volume(37)
        self.instance.play()
        current = self.instance._volume_control
        self.assertNotEqual(old.target, current.target)
        command = self.spawn.call_args.args[0]
        self.assertIn(current.target + '=0.37,', command[command.index('-af') + 1])
        self.assertEqual(command[command.index('-volume') + 1], '100')
        self.assertIn(r"azmq=bind_address='tcp\://127.0.0.1\:", command[command.index('-af') + 1])
        generation = self.instance._generation
        self.instance._notify(generation - 1, 'error', 'old startup error')
        self.assertIs(self.instance._volume_control, current)
        self.assertFalse(current._closed)
        self.instance.close()
        self.assertTrue(current._closed)

    def test_speed_filter_maps_output_clock_to_source_and_retains_position(self):
        # Measured supplied ffplay: atempo keeps the seek offset but rescales
        # elapsed audio timestamps, e.g. seek=3/speed=2 ends near raw 5.4, not 8.
        for speed in (0.5, 1, 1.5, 2):
            with self.subTest(speed=speed):
                self.instance.pause()
                self.instance.seek(3)
                self.instance.set_speed(speed)
                self.assertFalse(self.instance.is_playing)
                self.assertEqual(self.instance.position, 3)
                self.instance.play()
                command = self.spawn.call_args.args[0]
                self.assertIn('-af', command, 'pitch-preserving tempo filter missing')
                filters = [item for item in command[command.index('-af') + 1].split(',')
                           if item.startswith('atempo=')]
                factors = []
                for item in filters:
                    self.assertTrue(item.startswith('atempo='), item)
                    factor = float(item.split('=')[1])
                    self.assertGreaterEqual(factor, 0.5)
                    self.assertLessEqual(factor, 2)
                    factors.append(factor)
                product = 1
                for factor in factors:
                    product *= factor
                self.assertAlmostEqual(product, speed)
                self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file,pipe')
                process = self.processes[-1]
                process.emit(b'   2.97 M-A: 0.000 fd= 0\r')
                time.sleep(0.02)
                self.assertEqual(self.instance.position, 3, 'startup latency must not rewind saved source')
                process.emit(b'   3.50 M-A: 0.000 fd= 0\r')
                expected = 3 + 0.5 * speed
                eventually(lambda: self.instance.position == expected)
                time.sleep(0.02)
                self.assertEqual(self.instance.position, expected, 'stalled clock must not advance')
                self.assertEqual(self.instance.duration, 8, 'duration remains source seconds')
                generation = self.instance._generation
                new_speed = 1.5 if speed == 1 else 1
                self.instance.set_speed(new_speed)
                self.assertTrue(self.instance.is_playing)
                self.assertEqual(self.instance.position, expected)
                command = self.spawn.call_args.args[0]
                self.assertEqual(float(command[command.index('-ss') + 1]), expected)
                self.instance._notify(generation, 'ended', None)
                self.instance._notify(generation, 'error', 'stale filter error')
                self.assertEqual(self.instance.position, expected)
                self.assertTrue(self.instance.is_playing)
                calls = self.spawn.call_count
                self.instance.set_speed(new_speed)
                self.assertEqual(self.spawn.call_count, calls)
                current = self.processes[-1]
                current.emit(('{:.2f} M-A: 0.000 fd= 0\r'.format(expected + 0.2)).encode())
                eventually(lambda: abs(self.instance.position - (expected + 0.2 * new_speed)) < 1e-8)
        self.instance.pause()
        self.assertEqual(self.events, [])

    def test_speed_change_discards_old_buffered_clock_and_eof(self):
        self.instance.set_speed(0.5)
        self.instance.play()
        old = self.processes[-1]
        old.emit(b'   4.00 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 3.5)
        old.terminate = lambda: (setattr(old, 'returncode', 0), old.done.set())
        self.instance.set_speed(2)
        old.emit(b'  12.70 M-A: 0.000 fd= 0\r')
        old.emit(b'')
        eventually(lambda: old.stderr.closed)
        self.assertEqual(self.instance.position, 3.5)
        self.assertTrue(self.instance.is_playing)
        self.assertEqual(self.events, [])
        current = self.processes[-1]
        current.emit(b'   3.75 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 3.5 + .25 * 2)
        # The last output clock need not equal source duration at accelerated EOF.
        current.finish()
        eventually(lambda: bool(self.events))
        self.assertEqual(self.events[0][:2], ('ended', None))
        self.assertEqual(self.instance.position, 8)
        self.instance.set_speed(2)
        self.assertEqual(self.instance.position, 8)
        self.assertFalse(self.instance.is_playing)

    def test_live_volume_while_accelerated_keeps_source_clock_mapping(self):
        self.instance.set_speed(2)
        self.instance.play()
        self.processes[-1].emit(b'   3.50 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 4)
        self.instance.set_volume(50)
        command = self.spawn.call_args.args[0]
        self.assertEqual(float(command[command.index('-ss') + 1]), 3)
        self.assertTrue(command[command.index('-af') + 1].startswith('atempo=2,'))
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(self.instance.speed, 2)
        self.assertEqual(self.instance.position, 4)
        self.processes[-1].emit(b'   4.00 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 5)
        self.instance.seek(1.25)
        self.assertEqual(self.instance.position, 1.25)
        self.processes[-1].emit(b'   1.50 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 1.75)

    def test_seek_to_end_does_not_spawn_empty_stream_or_emit_ended(self):
        self.instance.play()
        self.instance.seek(999)
        self.assertEqual(self.instance.position, 8)
        self.assertFalse(self.instance.is_playing)
        self.assertEqual(self.spawn.call_count, 1)
        self.instance.play()
        command = self.spawn.call_args.args[0]
        self.assertEqual(float(command[command.index('-ss') + 1]), 0,
                         'play at EOF should replay from start')
        self.instance.close()
        self.assertEqual(self.events, [])

    def test_old_worker_cannot_overwrite_new_seek_position(self):
        self.instance.play()
        old = self.processes[-1]
        # Hold EOF so an obsolete worker can still report buffered statistics.
        old.terminate = lambda: (setattr(old, 'returncode', 0), old.done.set())
        self.instance.seek(6)
        old.emit(b'   1.25 M-A: 0.000 fd= 0\r')
        old.emit(b'')
        eventually(lambda: old.stderr.closed)
        self.assertEqual(self.instance.position, 6)
        self.instance.close()
        self.assertEqual(self.events, [])

    def test_ended_callback_can_reentrantly_load_and_play_next_chapter(self):
        completed = threading.Event()
        result = subprocess.CompletedProcess([], 0, '{"format":{"duration":"8"}}', '')
        def next_chapter(event, detail):
            self.events.append((event, detail))
            if event == 'ended':
                with mock.patch.object(player.subprocess, 'run', return_value=result):
                    self.instance.load(__file__)
                self.instance.play()
                completed.set()
        self.instance._on_event = next_chapter
        self.instance.play()
        self.instance.play()
        self.assertEqual(self.spawn.call_count, 1)
        self.processes[-1].emit(b'   7.94 M-A: 0.000 fd= 0\r')
        self.processes[-1].finish()
        self.assertTrue(completed.wait(2), 'callback deadlocked')
        self.assertTrue(self.instance.is_playing)
        self.assertEqual(self.instance.position, 0)
        self.assertEqual(self.spawn.call_count, 2)
        self.instance.close()
        self.assertEqual(self.events, [('ended', None)])

    def test_decoder_failure_reports_string_error_not_ended(self):
        self.instance.play()
        process = self.processes[-1]
        process.emit(b'Invalid data found when processing input')
        process.finish(1)
        eventually(lambda: bool(self.events))
        self.assertEqual(self.events[0][0], 'error')
        self.assertIsInstance(self.events[0][1], str)
        self.assertIn('Invalid data', self.events[0][1])
        self.assertNotEqual(self.events[0][2], threading.get_ident())
        self.assertFalse(self.instance.is_playing)
        self.assertEqual(self.instance.position, 3)

    def test_zero_exit_without_audio_is_error_not_natural_end(self):
        self.instance.play()
        process = self.processes[-1]
        process.emit(b'Failed to open file or configure filtergraph\n')
        process.finish(0)
        eventually(lambda: bool(self.events))
        self.assertEqual(self.events[0][0], 'error')
        self.assertIn('Failed to open', self.events[0][1])

    def test_missing_executable_reports_error_on_worker(self):
        self.spawn.side_effect = FileNotFoundError('ffplay.exe missing')
        try:
            self.instance.play()
        except OSError as error:
            self.fail('play must report launch failure through worker callback: ' + str(error))
        eventually(lambda: bool(self.events))
        self.assertEqual(self.events[0][0], 'error')
        self.assertIn('missing', self.events[0][1])
        self.assertNotEqual(self.events[0][2], threading.get_ident())
        self.assertFalse(self.instance.is_playing)

    def test_eof_with_hung_process_is_killed_reaped_and_reports_error(self):
        with mock.patch.object(player, 'PROCESS_TIMEOUT', 0.02):
            self.instance.play()
            process = self.processes[-1]
            process.emit(b'')
            eventually(lambda: bool(self.events))
        self.assertTrue(process.killed)
        self.assertIsNotNone(process.poll())
        self.assertEqual(self.events[0][0], 'error')
        self.assertTrue(process.stderr.closed)

    def test_load_replaces_playing_process_without_old_callback(self):
        self.instance.play()
        old = self.processes[-1]
        old.terminate = lambda: old.finish(0)
        result = subprocess.CompletedProcess([], 0, '{"format":{"duration":"15"}}', '')
        with mock.patch.object(player.subprocess, 'run', return_value=result):
            self.instance.load(__file__, position=1)
        self.assertFalse(self.instance.is_playing)
        self.assertIsNotNone(old.poll())
        self.assertEqual(self.instance.duration, 15)
        self.assertEqual(self.instance.position, 1)
        self.instance.close()
        self.assertEqual(self.events, [])

    def test_pause_resume_and_seek_suppress_old_generation_end(self):
        self.assertTrue(hasattr(self.instance, 'pause'), 'pause missing')
        self.instance.play()
        first = self.processes[-1]
        first.emit(b'   3.50 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 3.5)
        # A clean exit racing with pause/seek must not cause auto-next.
        first.terminate = lambda: first.finish(0)
        self.instance.pause()
        self.assertFalse(self.instance.is_playing)
        self.assertEqual(self.instance.position, 3.5)
        self.instance.play()
        resumed = self.processes[-1]
        command = self.spawn.call_args.args[0]
        self.assertEqual(float(command[command.index('-ss') + 1]), 3.5)
        self.instance.seek(6)
        self.assertTrue(resumed.terminated)
        self.assertEqual(self.instance.position, 6)
        current = self.processes[-1]
        self.assertIsNot(current, resumed)
        self.assertTrue(self.instance.is_playing)
        current.emit(b'   6.23 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 6.23)
        self.instance.pause()
        self.instance.seek(-100)
        self.assertEqual(self.instance.position, 0)
        self.instance.seek(100)
        self.assertEqual(self.instance.position, 8)
        self.assertFalse(self.instance.is_playing)
        self.instance.stop()
        self.assertEqual(self.instance.position, 0)
        self.instance.close()
        self.assertEqual(self.events, [])

    def test_stop_kills_and_reaps_uncooperative_child(self):
        self.assertTrue(hasattr(self.instance, 'stop'), 'stop missing')
        self.instance.play()
        process = self.processes[-1]
        process.terminate = lambda: None
        with mock.patch.object(player, 'PROCESS_TIMEOUT', 0.02):
            self.instance.stop()
        self.assertTrue(process.killed)
        self.assertIsNotNone(process.poll())
        self.assertTrue(all(t is not None for t in process.waits))
        self.assertEqual(self.instance.position, 0)

    def test_close_is_idempotent_and_blocks_reuse(self):
        self.assertTrue(hasattr(self.instance, 'close'), 'close missing')
        self.instance.play()
        self.instance.close()
        self.instance.close()
        self.assertFalse(self.instance.is_playing)
        with self.assertRaises(RuntimeError):
            self.instance.play()
        with self.assertRaises(RuntimeError):
            self.instance.load(__file__)
        self.assertEqual(self.events, [])

    def test_play_uses_real_clock_chunks_and_natural_end_event_from_worker(self):
        self.assertTrue(hasattr(self.instance, 'play'), 'play missing')
        self.instance.play()
        self.assertTrue(self.instance.is_playing)
        process = self.processes[-1]
        command = self.spawn.call_args.args[0]
        for flag in ('-nodisp', '-autoexit', '-stats'):
            self.assertIn(flag, command)
        self.assertIn('-protocol_whitelist', command)
        whitelist = command.index('-protocol_whitelist')
        self.assertEqual(command[whitelist + 1], 'file,pipe')
        self.assertLess(whitelist + 1, len(command) - 1)
        self.assertEqual(command[-1], str(Path(__file__).resolve()))
        self.assertEqual(command[command.index('-volume') + 1], '100')
        self.assertIn(self.instance._volume_control.target + '=0,', command[command.index('-af') + 1])
        self.assertEqual(float(command[command.index('-ss') + 1]), 3)
        kwargs = self.spawn.call_args.kwargs
        self.assertFalse(kwargs.get('shell', False))
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        time.sleep(0.04)
        self.assertEqual(self.instance.position, 3, 'no wall-clock guessing before audio starts')
        process.emit(b'    nan M-A: nan fd= 0\r   3.')
        process.emit(b'42 M-A: 0.000 fd= 0\r')
        eventually(lambda: self.instance.position == 3.42)
        time.sleep(0.04)
        self.assertEqual(self.instance.position, 3.42, 'stalled audio must not advance')
        process.finish()
        eventually(lambda: bool(self.events))
        self.assertEqual(self.events[0][:2], ('ended', None))
        self.assertNotEqual(self.events[0][2], threading.get_ident())
        self.assertFalse(self.instance.is_playing)
        self.assertEqual(self.instance.position, 8)
        self.assertTrue(process.stderr.closed)


if __name__ == "__main__":
    unittest.main()
