"""Native Player integration; fake transport only, real parser/control ownership."""
import importlib.util
import inspect
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.test-work' / 'player-native'
WORK.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('native_test_player', ROOT / 'addon/globalPlugins/czytelnia/player.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('condition timeout')


class Pipe:
    def __init__(self):
        self.chunks = queue.Queue()
        self.closed = False

    def read1(self, size):
        return self.chunks.get(timeout=5)

    def close(self):
        self.closed = True


class Process:
    def __init__(self, args, *, cwd):
        self.args, self.cwd = args, Path(cwd)
        self.pid = id(self)
        self.stderr = Pipe()
        self.returncode = None
        self.done = threading.Event()
        self.closed = False
        self.seeks = []
        self.on_seek = lambda seconds: self.clock(seconds)
        start = float(args[args.index('-ss') + 1])
        self.clock(start)

    def emit(self, text):
        self.stderr.chunks.put(text.encode())

    def clock(self, seconds, anchor=True):
        if anchor:
            self.emit(f'[ashowinfo@native_anchor @ 123] n:0 pts:{round(seconds * 48000)} fmt:fltp rate:48000\n')
        self.emit(f'{seconds:.5f} M-A: 0.0\r')

    def seek(self, seconds, duration):
        self.seeks.append((seconds, duration))
        self.on_seek(seconds)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('fake', timeout)
        return self.returncode

    def finish(self, code=0):
        self.returncode = code
        self.done.set()
        self.stderr.chunks.put(b'')

    def terminate(self):
        self.finish(1)

    kill = terminate

    def close(self):
        assert self.stderr.closed, 'close must follow the reader'
        self.closed = True


class NativePlayerTests(unittest.TestCase):
    def setUp(self):
        self.instances, self.processes, self.events = [], [], []
        self.control = mock.patch.object(p.VolumeControl, 'set_volume', return_value='0 OK').start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for instance in self.instances:
            instance.close()

    def spawn(self, args, *, cwd, env=None):
        process = Process(args, cwd=cwd)
        process.env = env
        self.processes.append(process)
        return process

    def make(self):
        self.assertIn('native_factory', inspect.signature(p.Player).parameters,
                      'native transport injection missing')
        instance = p.Player(ROOT / 'ffplay.exe', ROOT / 'ffprobe.exe',
                            lambda *e: self.events.append(e), volume=17,
                            native_factory=self.spawn)
        self.instances.append(instance)
        instance.load(__file__, position=10, duration=100)
        return instance

    def test_startup_waits_for_anchor_and_finite_clock_before_first_volume(self):
        instance = self.make()
        reading, returned = threading.Event(), threading.Event()
        errors, observed = [], []
        original_read = Pipe.read1

        def read(pipe, size):
            reading.set()
            return original_read(pipe, size)

        def play():
            try:
                instance.play()
            except BaseException as error:
                errors.append(error)
            finally:
                returned.set()

        self.control.side_effect = lambda value, **kw: observed.append(
            (value, instance._native_state.ready.is_set(), instance.position))
        with mock.patch.object(Process, 'clock'), mock.patch.object(Pipe, 'read1', read):
            caller = threading.Thread(target=play, daemon=True)
            caller.start()
            try:
                self.assertTrue(reading.wait(1))
                process = self.processes[-1]
                for text in ('10.0 M-A: 0.0\r',
                             '[ashowinfo@native_anchor @ 123] n:0 pts:480000 fmt:fltp rate:48000\n',
                             'nan M-A: nan\r'):
                    reading.clear()
                    process.emit(text)
                    self.assertTrue(reading.wait(1), 'watcher must keep draining during startup')
                    self.assertFalse(returned.wait(.03), 'play returned before a valid native clock')
                    self.control.assert_not_called()
                process.emit('10.25 M-A: 0.0\r')
                self.assertTrue(returned.wait(1))
            finally:
                if self.processes:
                    self.processes[-1].finish(1)
                caller.join(4)
                instance.close()
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, [(17, True, 10.25)])
        self.assertEqual(instance.volume, 17)
        self.assertFalse(instance._workers)
        self.assertTrue(process.closed)
        self.assertFalse(process.cwd.exists())

    def test_startup_decoder_error_wakes_barrier_without_connecting(self):
        instance = self.make()
        records = []
        instance._diagnostic_callback = lambda *record: records.append(record)
        message = 'Invalid data found when processing input'
        with mock.patch.object(Process, 'clock',
                               lambda process, *a, **kw: process.emit(message + '\n')), \
                mock.patch.object(p, 'PROCESS_TIMEOUT', 1):
            started = time.monotonic()
            instance.play()
            elapsed = time.monotonic() - started
        until(lambda: not instance._workers)
        self.control.assert_not_called()
        self.assertLess(elapsed, .5, 'decoder error must wake startup, not wait for a clock timeout')
        self.assertEqual(self.events, [('error', message)])
        self.assertEqual([r[0] for r in records], ['playback.start.readiness'])
        self.assertIn(message, records[0][1])
        self.assertNotIn('greeting_receive', records[0][1])
        self.assertFalse(instance.is_playing)
        self.assertEqual(instance.position, 10)
        self.assertTrue(self.processes[-1].closed)

    def test_startup_already_drained_clean_eof_skips_control_and_notifies_once(self):
        instance = self.make()
        records = []
        instance._diagnostic_callback = lambda *record: records.append(record)
        original_state = p._NativePlayback
        original_clock = Process.clock

        def state(speed):
            native = original_state(speed)
            # Hold the caller until the watcher publishes its complete outcome;
            # dispatch remains blocked by play's operations lock.
            native.startup.wait = lambda timeout: native.drained.wait(timeout)
            return native

        def short_tail(process, seconds):
            original_clock(process, seconds)
            process.finish(0)

        with mock.patch.object(p, '_NativePlayback', side_effect=state), \
                mock.patch.object(Process, 'clock', short_tail):
            instance.play()
        until(lambda: not instance._workers)
        self.control.assert_not_called()
        self.assertEqual(self.events, [('ended', None)])
        self.assertEqual(records, [])
        self.assertEqual(instance.position, 100)
        self.assertFalse(instance.is_playing)
        self.assertTrue(self.processes[-1].closed)
        instance._notify(instance._generation, 'ended', None)
        self.assertEqual(self.events, [('ended', None)])

    def test_startup_drained_failure_wakes_without_clock_or_control(self):
        for code in (0, 7):
            with self.subTest(code=code):
                instance = self.make()
                records = []
                instance._diagnostic_callback = lambda *r: records.append(r)
                self.events.clear()
                self.control.reset_mock()

                def exit_without_clock(process, seconds):
                    process.emit('nan M-A: nan\r')
                    process.finish(code)

                with mock.patch.object(Process, 'clock', exit_without_clock), \
                        mock.patch.object(p, 'PROCESS_TIMEOUT', 1):
                    started = time.monotonic()
                    instance.play()
                    elapsed = time.monotonic() - started
                until(lambda: not instance._workers)
                self.assertLess(elapsed, .5, 'drained failure must wake readiness')
                self.control.assert_not_called()
                self.assertEqual(self.events, [('error', f'ffplay exited with code {code}: nan M-A: nan')])
                self.assertEqual([r[0] for r in records], ['playback.start.readiness'])
                self.assertFalse(instance.is_playing)
                self.assertEqual(instance.position, 10)
                self.assertTrue(self.processes[-1].closed)

    def test_startup_timeout_never_connects_and_late_eof_stays_stale(self):
        instance = self.make()
        records = []
        instance._diagnostic_callback = lambda *r: records.append(r)
        original_read = Pipe.read1
        release = threading.Event()

        def delayed_read(pipe, size):
            release.wait(2)
            return original_read(pipe, size)

        with mock.patch.object(Pipe, 'read1', delayed_read), \
                mock.patch.object(p, 'PROCESS_TIMEOUT', .03):
            try:
                started = time.monotonic()
                instance.play()
                self.assertLess(time.monotonic() - started, .5)
                until(lambda: bool(self.events))
                self.control.assert_not_called()
                self.assertFalse(instance.is_playing)
                self.assertEqual(instance.position, 10)
                self.assertEqual(self.events, [('error', 'No initial native audio clock')])
                self.assertEqual([r[0] for r in records], ['playback.start.readiness'])
                self.assertNotIn('greeting_receive', records[0][1])
            finally:
                release.set()
                until(lambda: not instance._workers)
        self.assertTrue(self.processes[-1].closed)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(instance.position, 10)
        # A fresh generation still starts normally and preserves the saved gain.
        instance.play()
        self.control.assert_called_once()
        self.assertEqual(self.control.call_args.args[0], 17)
        self.assertTrue(instance._native_state.ready.is_set())
        self.assertTrue(instance.is_playing)

    def test_close_during_startup_has_bounded_wait_and_no_worker_or_gain_leak(self):
        instance = self.make()
        waiting, closed = threading.Event(), threading.Event()
        original_state = p._NativePlayback
        errors = []

        def state(speed):
            native = original_state(speed)
            wait = native.startup.wait
            def await_start(timeout):
                waiting.set()
                return wait(timeout)
            native.startup.wait = await_start
            return native

        def run(action):
            try:
                action()
            except BaseException as error:
                errors.append(error)

        def close():
            instance.close()
            closed.set()

        with mock.patch.object(Process, 'clock'), \
                mock.patch.object(p, '_NativePlayback', side_effect=state), \
                mock.patch.object(p, 'PROCESS_TIMEOUT', .1):
            caller = threading.Thread(target=run, args=(instance.play,), daemon=True)
            closer = threading.Thread(target=run, args=(close,), daemon=True)
            caller.start()
            try:
                self.assertTrue(waiting.wait(1))
                closer.start()
                self.assertTrue(closed.wait(1), 'close blocked indefinitely behind startup')
            finally:
                caller.join(2)
                if closer.ident is not None:
                    closer.join(2)
                instance.close()
        self.assertEqual(errors, [])
        self.assertFalse(caller.is_alive())
        self.assertFalse(closer.is_alive())
        self.control.assert_not_called()
        self.assertFalse(instance._workers)
        self.assertTrue(self.processes[-1].closed)
        self.assertFalse(self.processes[-1].cwd.exists())
        self.assertNotIn(('ended', None), self.events)
        before = list(self.events)
        instance._notify(instance._generation - 1, 'ended', None)
        self.assertEqual(self.events, before)

    def test_paused_end_and_unchanged_seek_do_not_spawn_or_emit_eof(self):
        instance = self.make()
        callbacks = []
        instance.seek(20, before_seek=lambda: callbacks.append('paused'))
        self.assertEqual(instance.position, 20)
        self.assertFalse(self.processes)
        instance.play()
        process = self.processes[-1]
        until(lambda: instance._native_state.ready.is_set())
        instance.seek(instance.position, before_seek=lambda: callbacks.append('unchanged'))
        self.assertEqual(process.seeks, [])
        instance.seek(1000, before_seek=lambda: callbacks.append('end'))
        self.assertEqual(instance.position, 100)
        self.assertFalse(instance.is_playing)
        until(lambda: not instance._workers)
        self.assertEqual(callbacks, ['paused', 'unchanged', 'end'])
        self.assertEqual(self.events, [])

    def test_native_eof_callback_can_load_and_play_next_part_reentrantly(self):
        instance = self.make()
        done = threading.Event()
        def callback(event, detail):
            self.events.append((event, detail))
            if event == 'ended':
                instance.load(__file__, 30, duration=100)
                instance.play()
                done.set()
        instance._on_event = callback
        instance.play()
        first = self.processes[-1]
        until(lambda: instance._native_state.ready.is_set())
        first.finish(0)
        self.assertTrue(done.wait(2), 'reentrant EOF deadlocked')
        self.assertEqual(len(self.processes), 2)
        self.assertEqual(instance.position, 30)
        self.assertTrue(instance.is_playing)
        self.assertTrue(first.closed)
        self.assertEqual(self.events, [('ended', None)])

    def test_old_reader_cannot_publish_clock_after_resume(self):
        instance = self.make()
        instance.play()
        old = self.processes[-1]
        until(lambda: instance._native_state.ready.is_set())
        def no_eof():
            old.returncode = 0
            old.done.set()
        old.terminate = no_eof
        instance.pause()
        instance.seek(30)
        instance.play()
        old.clock(90)
        old.stderr.chunks.put(b'')
        until(lambda: old.closed)
        self.assertEqual(instance.position, 30)
        self.assertTrue(instance.is_playing)
        self.assertEqual(self.events, [])

    def test_native_startup_ack_error_preserves_proven_short_tail_eof(self):
        instance = self.make()
        def reject(*a, **kw):
            process = self.processes[-1]
            process.finish(0)
            raise p.ControlError('exited before ack')
        self.control.side_effect = reject
        instance.play()
        until(lambda: not instance._workers)
        self.assertEqual(self.events, [('ended', None)])
        self.assertEqual(instance.position, 100)
        self.assertTrue(self.processes[-1].closed)

    def test_deferred_reader_keeps_pipe_owned_until_drain_then_cleans_directory(self):
        instance = self.make()
        instance.play()
        process = self.processes[-1]
        until(lambda: instance._native_state.ready.is_set())
        # Simulate delayed pipe EOF after the child has exited. Never close the
        # pipe under its active reader, even when the public close wait expires.
        def terminate_without_pipe_eof():
            process.returncode = 1
            process.done.set()
        process.terminate = terminate_without_pipe_eof
        directory = process.cwd
        with mock.patch.object(p, 'PROCESS_TIMEOUT', .02):
            instance.close()
        self.assertFalse(process.closed)
        self.assertFalse(process.stderr.closed)
        process.stderr.chunks.put(b'')
        until(lambda: process.closed and not instance._workers)
        self.assertFalse(directory.exists(), 'deferred final owner must remove its directory')

    def test_actual_missing_gain_diagnostic_fails_seek_without_waiting_for_timeout(self):
        instance = self.make()
        instance.play()
        process = self.processes[-1]
        process.on_seek = lambda target: process.emit("[Parsed_asendcmd_2 @ 123] [FILE @ 123] Cannot read file 'gain.cmd': No such file or directory\n")
        with mock.patch.object(p, 'SEEK_TIMEOUT', .2):
            with self.assertRaisesRegex(RuntimeError, 'Cannot read file'):
                instance.seek(50)
        self.assertFalse(instance.is_playing)

    def test_child_software_renderer_does_not_change_parent_or_audio_driver(self):
        instance = self.make()
        with mock.patch.dict(os.environ, {'SDL_RENDER_DRIVER': 'direct3d',
                                        'SDL_AUDIODRIVER': 'wasapi', 'SDL_VIDEODRIVER': 'dummy'}):
            instance.play()
            env = self.processes[-1].env
            self.assertIsNotNone(env, 'native child needs isolated SDL configuration')
            self.assertEqual(env['SDL_RENDER_DRIVER'], 'software')
            self.assertEqual(env['SDL_VIDEODRIVER'], 'windows')
            self.assertEqual(env['SDL_AUDIODRIVER'], 'wasapi')
            self.assertEqual(os.environ['SDL_RENDER_DRIVER'], 'direct3d')
            self.assertEqual(os.environ['SDL_VIDEODRIVER'], 'dummy')

    def test_nonzero_mp3_timestamp_origin_is_not_saved_as_source_progress(self):
        instance = self.make()
        instance.set_speed(2)
        instance.play()
        process = self.processes[-1]
        until(lambda: instance._native_state.ready.is_set())
        process.emit('  Duration: 00:01:40.00, start: 0.025, bitrate: 64 kb/s\n')
        process.clock(10.025)
        process.clock(10.125, anchor=False)
        until(lambda: abs(instance.position - 10.2) < .0001)
        self.assertEqual(instance._native_state.origin, .025)

    def test_volume_file_commits_before_live_command_and_write_failure_is_atomic(self):
        instance = self.make()
        instance.play()
        process = self.processes[-1]
        control = instance._volume_control
        path = process.cwd / 'gain.cmd'
        before = path.read_bytes()
        observed = []
        self.control.side_effect = lambda value, **kw: observed.append(path.read_text())
        with mock.patch.object(p.os, 'replace', side_effect=PermissionError('locked file')):
            with self.assertRaises(PermissionError):
                instance.set_volume(37)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(instance.volume, 17)
        self.assertEqual(observed, [])
        self.assertIs(instance._process, process)
        instance.set_volume(37)
        self.assertEqual(observed, [f'0.0 {control.target} volume 0.37;\n'])
        instance.seek(50)
        self.assertEqual(path.read_text(), observed[0])
        instance.set_volume(1)
        self.assertEqual(path.read_text(), f'0.0 {control.target} volume 0.01;\n')
        self.assertEqual(instance.volume, 1)
        self.assertIs(instance._process, process)

    def test_volume_ack_failure_preserves_already_drained_natural_eof(self):
        instance = self.make()
        instance.load(__file__, position=99.9, duration=100)
        instance.play()
        process = self.processes[-1]
        native = instance._native_state
        until(lambda: native.ready.is_set())
        generation = instance._generation

        def reject(*args, **kwargs):
            process.finish(0)
            self.assertTrue(native.drained.wait(2))
            raise p.ControlError('exited before volume ACK')

        self.control.side_effect = reject
        instance.set_volume(37)
        until(lambda: not instance._workers)
        self.assertEqual(self.events, [('ended', None)])
        self.assertEqual(instance.position, 100)
        self.assertEqual(instance.volume, 17)
        self.assertEqual(instance._generation, generation)
        self.assertFalse(instance.is_playing)
        self.assertTrue(process.closed)

    def test_volume_ack_failure_waits_for_delayed_eof_drain(self):
        instance = self.make()
        instance.load(__file__, position=99.9, duration=100)
        instance.play()
        process = self.processes[-1]
        native = instance._native_state
        until(lambda: native.ready.is_set())
        wait = native.drained.wait

        def reject(*args, **kwargs):
            process.returncode = 0
            process.done.set()
            self.assertFalse(native.drained.is_set())
            raise p.ControlError('exit before pipe drain')

        def drain(timeout):
            # Release pipe EOF only when the command starts waiting for it.
            process.stderr.chunks.put(b'')
            return wait(timeout)

        self.control.side_effect = reject
        try:
            with mock.patch.object(native.drained, 'wait', side_effect=drain) as waited:
                instance.set_volume(37)
                waited.assert_called_once_with(p.PROCESS_TIMEOUT)
        finally:
            process.stderr.chunks.put(b'')
        until(lambda: not instance._workers)
        self.assertEqual(self.events, [('ended', None)])
        self.assertEqual(instance.position, 100)
        self.assertEqual(instance.volume, 17)
        self.assertFalse(instance.is_playing)
        self.assertTrue(process.closed)

    def test_volume_ack_failure_eof_callback_can_advance_or_pause_reentrantly(self):
        for action in ('advance', 'pause'):
            with self.subTest(action=action):
                self.control.side_effect = None
                instance = self.make()
                instance.load(__file__, position=99.9, duration=100)
                instance.play()
                process = self.processes[-1]
                native = instance._native_state
                until(lambda: native.ready.is_set())
                observed = []

                def callback(event, detail):
                    observed.append((event, detail, instance.position, instance.volume))
                    if action == 'advance':
                        instance.load(__file__, position=30, duration=100)
                        instance.play()
                    else:
                        instance.pause()

                def reject(*args, **kwargs):
                    process.finish(0)
                    self.assertTrue(native.drained.wait(2))
                    self.control.side_effect = None
                    raise p.ControlError('exited before volume ACK')

                instance._on_event = callback
                self.control.reset_mock()
                self.control.side_effect = reject
                old_workers = tuple(instance._workers)
                instance.set_volume(37)
                for worker in old_workers:
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(observed, [('ended', None, 100, 17)])
                self.assertEqual(instance.volume, 17)
                self.assertTrue(process.closed)
                if action == 'advance':
                    next_process = self.processes[-1]
                    self.assertIsNot(next_process, process)
                    self.assertIs(instance._process, next_process)
                    self.assertEqual(instance.position, 30)
                    self.assertEqual([call.args[0] for call in self.control.call_args_list], [37, 17])
                    self.assertEqual((next_process.cwd / 'gain.cmd').read_text(),
                                     f'0.0 {instance._volume_control.target} volume 0.17;\n')
                else:
                    self.assertFalse(instance.is_playing)
                    self.assertEqual(instance.position, 100)
                    self.assertEqual(self.control.call_count, 1)
                instance.close()

    def test_volume_ack_failure_does_not_treat_unproven_exit_as_eof(self):
        # No-clock startup is rejected before any runtime command is possible;
        # covered separately by the startup readiness tests.
        for mode in ('corrupt', 'nonzero', 'undrained'):
            with self.subTest(mode=mode):
                self.control.side_effect = None
                instance = self.make()
                instance.play()
                until(lambda: instance._native_state.ready.is_set())
                process = self.processes[-1]
                native = instance._native_state

                def reject(*args, **kwargs):
                    if mode == 'corrupt':
                        process.emit('Invalid data found when processing input\n')
                    if mode == 'undrained':
                        process.returncode = 0
                        process.done.set()
                    else:
                        process.finish(1 if mode == 'nonzero' else 0)
                        self.assertTrue(native.drained.wait(2))
                        self.assertEqual(native.outcome[0], 'error')
                    raise p.ControlError('unproven exit')

                self.control.side_effect = reject
                try:
                    with mock.patch.object(p, 'PROCESS_TIMEOUT', .02):
                        with self.assertRaisesRegex(p.ControlError, 'unproven exit'):
                            instance.set_volume(37)
                finally:
                    process.stderr.chunks.put(b'')
                until(lambda: not instance._workers)
                self.assertFalse(instance.is_playing)
                self.assertEqual(instance.volume, 17)
                self.assertEqual(instance.position, 10)
                self.assertTrue(process.closed)
                self.assertEqual(self.events, [])

    def test_volume_ack_failure_stops_native_with_previous_setting(self):
        instance = self.make()
        instance.play()
        process = self.processes[-1]
        self.control.side_effect = p.ControlError('uncertain ACK')
        with self.assertRaises(p.ControlError):
            instance.set_volume(37)
        self.assertFalse(instance.is_playing, 'uncertain applied gain must not keep playing')
        self.assertEqual(instance.volume, 17)
        until(lambda: process.closed)
        self.assertEqual(self.events, [])

    def test_pending_old_eof_is_delivered_once_before_seek_barrier(self):
        instance = self.make()
        instance.play()
        process = self.processes[-1]
        until(lambda: instance._native_state.ready.is_set())
        order = []
        instance._on_event = lambda event, detail: order.append(event)
        with instance._operations:
            process.finish(0)
            until(lambda: process.closed)
            instance.seek(20, before_seek=lambda: order.append('barrier'))
        until(lambda: not instance._workers)
        self.assertEqual(order, ['ended', 'barrier'])
        self.assertEqual(instance.position, 20)
        self.assertFalse(instance.is_playing)

    def test_seek_failures_stop_without_eof_or_publishing_old_clocks(self):
        for mode in ('post', 'demux', 'timeout', 'exit', 'filter'):
            with self.subTest(mode=mode):
                instance = self.make()
                instance.play()
                process = self.processes[-1]
                until(lambda: instance._native_state.ready.is_set())
                callbacks = []
                def fail(target):
                    if mode == 'post':
                        raise OSError('post failed')
                    if mode == 'demux':
                        process.emit('error while seeking\n')
                    if mode == 'filter':
                        process.emit('Error initializing filter asendcmd: No such file\n')
                    process.clock(90, anchor=False)
                    if mode == 'exit':
                        process.finish(0)
                process.on_seek = fail
                with mock.patch.object(p, 'SEEK_TIMEOUT', .1):
                    with self.assertRaises((OSError, RuntimeError)):
                        instance.seek(50, before_seek=lambda: callbacks.append('barrier'))
                self.assertFalse(instance.is_playing, 'failed seek must fail closed')
                self.assertEqual(callbacks, ['barrier'])
                self.assertEqual(instance.position, 10)
                until(lambda: not instance._workers)
                self.assertTrue(process.closed)
                self.assertEqual(self.events, [])

    def test_graph_reset_discards_socket_but_keeps_endpoint_available(self):
        control = p.VolumeControl()
        self.addCleanup(control.close)
        self.assertTrue(hasattr(control, 'reset_connection'), 'graph reset API missing')
        endpoint = (control.port, control.target)
        sock = mock.Mock()
        control._socket = sock
        control.reset_connection()
        sock.close.assert_called_once()
        self.assertIsNone(control._socket)
        self.assertFalse(control._closed)
        self.assertEqual((control.port, control.target), endpoint)
        control.close()
        with self.assertRaises(p.ControlError):
            control.reset_connection()

    def test_live_seek_uses_fresh_anchor_and_can_move_backward_same_pid(self):
        instance = self.make()
        instance.set_speed(2)
        instance.play()
        process = self.processes[-1]
        until(lambda: instance.position >= 10)
        observed = []
        process.on_seek = lambda target: (observed.append(('post', instance.position)), process.clock(target + .1))
        generation = instance._generation
        instance.seek(50, before_seek=lambda: observed.append(('barrier', instance.position)))
        self.assertEqual([item[0] for item in observed], ['barrier', 'post'])
        self.assertAlmostEqual(instance.position, 50.1)
        process.clock(50.3, anchor=False)
        until(lambda: abs(instance.position - 50.5) < .001)
        instance.seek(20)
        self.assertAlmostEqual(instance.position, 20.1)
        self.assertIs(instance._process, process)
        self.assertEqual(instance._generation, generation)
        self.assertEqual(self.control.call_count, 1, 'seeks must not issue gain commands')
        self.assertEqual(self.events, [])

    def test_native_launch_file_initializes_gain_before_first_frame(self):
        instance = self.make()
        self.assertTrue(instance.supports_live_seek)
        instance.play()
        process = self.processes[-1]
        graph = process.args[process.args.index('-af') + 1]
        self.assertNotIn('-nodisp', process.args)
        self.assertTrue(graph.startswith('ashowinfo@native_anchor,atempo=1,'))
        self.assertIn('asendcmd=filename=gain.cmd,' + instance._volume_control.target + '=0,', graph)
        self.assertEqual((process.cwd / 'gain.cmd').read_text(),
                         f'0.0 {instance._volume_control.target} volume 0.17;\n')
        self.assertTrue(Path(process.args[0]).is_absolute())
        instance.close()
        self.assertTrue(process.closed)
        self.assertFalse(process.cwd.exists())


if __name__ == '__main__':
    unittest.main()
