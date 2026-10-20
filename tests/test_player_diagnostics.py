"""Error-only diagnostics: fake processes/transports, never audio or NVDA."""
import contextlib
import unittest
from unittest import mock

from test_player import FakeProcess, eventually, player

control = player._control_module
GREETING = control._GREETING
READY = b'\x05READY\x0bSocket-Type\0\0\0\x03REP'


def aborted_socket():
    error = ConnectionAbortedError(10053, 'connection aborted')
    error.winerror = 10053
    return error


class ControlDiagnosticTests(unittest.TestCase):
    def test_each_wire_failure_preserves_message_cause_and_stage(self):
        stages = ('tcp_connect', 'greeting_send', 'greeting_receive',
                  'ready_send', 'ready_receive', 'command_send',
                  'reply_delimiter', 'reply_body', 'reply_validate', 'peer_alive')
        for stage in stages:
            with self.subTest(stage=stage), contextlib.ExitStack() as stack:
                client = control.VolumeControl()
                self.addCleanup(client.close)
                peer = mock.Mock()
                create = stack.enter_context(mock.patch.object(control.socket, 'socket', return_value=peer))
                send = stack.enter_context(mock.patch.object(client, '_send'))
                read = stack.enter_context(mock.patch.object(client, '_read', return_value=GREETING))
                frame = stack.enter_context(mock.patch.object(client, '_frame', side_effect=[
                    (4, READY), (1, b''), (0, b'0 Success')]))
                error = aborted_socket()
                alive = lambda: True
                if stage == 'tcp_connect':
                    peer.connect.side_effect = error
                elif stage in ('greeting_send', 'ready_send', 'command_send'):
                    send.side_effect = [None] * ('greeting_send', 'ready_send', 'command_send').index(stage) + [error]
                elif stage == 'greeting_receive':
                    read.side_effect = error
                elif stage == 'ready_receive':
                    frame.side_effect = error
                elif stage == 'reply_delimiter':
                    frame.side_effect = [(4, READY), error]
                elif stage == 'reply_body':
                    frame.side_effect = [(4, READY), (1, b''), error]
                elif stage == 'reply_validate':
                    frame.side_effect = [(4, READY), (1, b''), (0, b'38 Nope')]
                elif stage == 'peer_alive':
                    alive = mock.Mock(side_effect=[True, True, False])
                with self.assertRaises(control.ControlError) as caught:
                    client.set_volume(37, alive=alive)
                failure = caught.exception
                self.assertEqual(getattr(failure, 'diagnostic_stage', None), stage)
                self.assertTrue(str(failure).startswith('Live volume control failed: '))
                if stage not in ('reply_validate', 'peer_alive'):
                    self.assertIs(failure.__cause__, error)
                    self.assertEqual(failure.errno, 10053)
                    self.assertEqual(failure.winerror, 10053)
                    self.assertEqual(str(failure), 'Live volume control failed: ' + str(error))
                self.assertTrue(client._closed)
                self.assertEqual(create.call_count, 1, 'failure must not reconnect')
                peer.close.assert_called_once()


class PlayerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.records, self.events = [], []
        self.process = FakeProcess()
        self.process.pid = 4321
        self.instance = player.Player('ffplay.exe', 'ffprobe.exe',
            lambda event, detail: self.events.append((event, detail)),
            use_native=False, diagnostic=lambda stage, detail: self.records.append((stage, detail)))
        self.addCleanup(self.instance.close)
        self.instance.load(__file__, position=3, duration=8)
        self.client = control.VolumeControl()
        self.addCleanup(self.client.close)
        self.stack = self.enterContext(contextlib.ExitStack())
        self.spawn = self.stack.enter_context(mock.patch.object(player.subprocess, 'Popen', return_value=self.process))
        self.stack.enter_context(mock.patch.object(player, 'VolumeControl', return_value=self.client))

    def test_startup_socket_abort_logged_once_with_trace_before_halt(self):
        peer = mock.Mock()
        peer.recv.side_effect = aborted_socket()
        self.process.emit(b'x' * 10000 + b'\nAZMQ_DIAGNOSTIC\n')
        with mock.patch.object(control.socket, 'socket', return_value=peer), \
                mock.patch.object(player, 'PROCESS_TIMEOUT', .03):
            self.instance.play()
            eventually(lambda: bool(self.events))
            eventually(lambda: not self.instance._workers)
        self.assertEqual(len(self.records), 1)
        stage, detail = self.records[0]
        self.assertEqual(stage, 'playback.start.volume')
        for expected in ('greeting_receive', 'errno=10053', 'winerror=10053',
                         'Traceback (most recent call last)', 'ConnectionAbortedError',
                         'pid=4321', 'returncode=None', 'position=3', 'speed=1',
                         'volume=100', 'AZMQ_DIAGNOSTIC'):
            self.assertIn(expected, detail)
        self.assertLess(len(detail), 20000)
        self.assertNotIn('x' * 8193, detail)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][0], 'error')
        self.assertNotIn('Traceback', self.events[0][1], 'spoken error must stay unchanged')
        self.assertTrue(self.process.terminated)
        self.assertFalse(self.instance.is_playing)
        self.assertEqual(self.spawn.call_count, 1)


    def test_runtime_volume_failure_reports_before_cleanup_and_reraises(self):
        with mock.patch.object(self.client, 'set_volume', return_value='0 Success'):
            self.instance.play()
        failure = player.ControlError('ack lost')  # No structured metadata required.
        with mock.patch.object(self.client, 'set_volume', side_effect=failure):
            with self.assertRaises(player.ControlError) as caught:
                self.instance.set_volume(37)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(self.records), 1)
        stage, detail = self.records[0]
        self.assertEqual(stage, 'playback.volume')
        self.assertIn('requested_volume=37', detail)
        self.assertIn('returncode=None', detail)
        self.assertIn('Traceback', detail)
        self.assertEqual(self.instance.volume, 100)
        self.assertEqual(self.events, [])
        self.assertTrue(self.instance.is_playing, 'legacy failure policy must not change')
        self.instance.stop()
        eventually(lambda: not self.instance._workers)
        self.assertEqual(len(self.records), 1)


    def test_decoder_failure_logs_once_without_changing_event(self):
        with mock.patch.object(self.client, 'set_volume', return_value='0 Success'):
            self.instance.play()
        self.process.emit(b'DECODER_BROKEN\n')
        self.process.finish(7)
        eventually(lambda: not self.instance._workers)
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0][0], 'playback.error')
        self.assertIn('returncode=7', self.records[0][1])
        self.assertIn('pid=4321', self.records[0][1])
        self.assertIn('DECODER_BROKEN', self.records[0][1])
        self.assertEqual(self.events, [('error', 'ffplay exited with code 7: DECODER_BROKEN')])
        self.instance._notify(self.instance._generation, 'error', 'duplicate')
        self.assertEqual(len(self.records), 1)

    def test_launch_failure_preserves_trace_and_emits_one_event(self):
        self.spawn.side_effect = FileNotFoundError(2, 'ffplay missing')
        self.instance.play()
        eventually(lambda: not self.instance._workers)
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0][0], 'playback.start')
        self.assertIn('Traceback', self.records[0][1])
        self.assertIn('errno=2', self.records[0][1])
        self.assertEqual(self.events, [('error', '[Errno 2] ffplay missing')])

    def test_watcher_io_failure_retains_original_trace(self):
        with mock.patch.object(self.client, 'set_volume', return_value='0 Success'), \
                mock.patch.object(self.process.stderr, 'read1', side_effect=OSError(5, 'pipe failed')):
            self.instance.play()
            eventually(lambda: not self.instance._workers)
        self.assertEqual(len(self.records), 1)
        self.assertIn('Traceback', self.records[0][1])
        self.assertIn('errno=5', self.records[0][1])
        self.assertTrue(self.process.killed)
        self.assertEqual(self.events, [('error', '[Errno 5] pipe failed')])


    def enable_native(self):
        # Exercise the native Player branch without a Win32 window or audio.
        self.instance._use_native = True
        self.instance._native_factory = lambda *args, **kwargs: self.process
        self.process.close = mock.Mock()
        self.process.emit(b'[ashowinfo@native_anchor @ 123] n:0 pts:144000 fmt:fltp rate:48000\n'
                          b'3.0 M-A: 0.0\r')

    def emit_clean_eof(self):
        self.process.emit(b'[ashowinfo@native_anchor @ 123] n:0 pts:336000 fmt:fltp rate:48000\n'
                          b'7.0 M-A: 0.0\r')
        self.process.finish(0)

    def test_native_startup_watcher_io_failure_retains_original_trace(self):
        self.enable_native()
        self.test_watcher_io_failure_retains_original_trace()
        self.assertEqual(self.records[0][0], 'playback.start.readiness')

    def test_native_runtime_failure_logs_before_halt_even_if_callback_fails(self):
        self.enable_native()
        attempted = []
        def failing_writer(stage, detail):
            attempted.append((stage, detail))
            raise OSError('log directory is read only')
        self.instance._diagnostic_callback = failing_writer
        with mock.patch.object(self.client, 'set_volume', return_value='0 Success'):
            self.instance.play()
        with mock.patch.object(self.client, 'set_volume', side_effect=player.ControlError('ack lost')):
            with self.assertRaisesRegex(player.ControlError, 'ack lost'):
                self.instance.set_volume(37)
        eventually(lambda: not self.instance._workers)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(attempted[0][0], 'playback.volume')
        self.assertIn('returncode=None', attempted[0][1])
        self.assertIn('requested_volume=37', attempted[0][1])
        self.assertTrue(self.process.terminated)
        self.assertFalse(self.instance.is_playing)
        self.assertEqual(self.instance.volume, 100)
        self.assertEqual(self.events, [])

    def test_callback_failure_cannot_replace_startup_error_or_prevent_stop(self):
        attempted = []
        def failing_writer(stage, detail):
            attempted.append(stage)
            raise RuntimeError('diagnostic failed')
        self.instance._diagnostic_callback = failing_writer
        with mock.patch.object(self.client, 'set_volume', side_effect=player.ControlError('ack lost')), \
                mock.patch.object(player, 'PROCESS_TIMEOUT', .03):
            self.instance.play()
            eventually(lambda: not self.instance._workers)
        self.assertEqual(attempted, ['playback.start.volume'])
        self.assertEqual(self.events, [('error', 'ack lost')])
        self.assertTrue(self.process.terminated)
        self.assertFalse(self.instance.is_playing)

    def test_startup_lost_ack_clean_eof_is_not_logged(self):
        def fail_ack(*args, **kwargs):
            self.emit_clean_eof()
            raise player.ControlError('ACK lost at EOF')
        with mock.patch.object(self.client, 'set_volume', side_effect=fail_ack):
            self.instance.play()
            eventually(lambda: not self.instance._workers)
        self.assertEqual(self.records, [])
        self.assertEqual(self.events, [('ended', None)])
        self.assertFalse(self.process.terminated)

    def test_native_startup_lost_ack_clean_eof_is_not_logged(self):
        self.enable_native()
        self.test_startup_lost_ack_clean_eof_is_not_logged()

    def test_native_runtime_lost_ack_clean_eof_is_not_logged(self):
        self.enable_native()
        with mock.patch.object(self.client, 'set_volume', return_value='0 Success'):
            self.instance.play()
        def fail_ack(*args, **kwargs):
            self.emit_clean_eof()
            raise player.ControlError('ACK lost at EOF')
        with mock.patch.object(self.client, 'set_volume', side_effect=fail_ack):
            self.instance.set_volume(37)
        eventually(lambda: not self.instance._workers)
        self.assertEqual(self.records, [])
        self.assertEqual(self.events, [('ended', None)])
        self.assertEqual(self.instance.volume, 100)
        self.assertFalse(self.process.terminated)

    def test_success_pause_and_stale_errors_are_not_logged(self):
        with mock.patch.object(self.client, 'set_volume', return_value='0 Success'):
            self.instance.play()
            self.instance.set_volume(37)
        generation = self.instance._generation
        self.instance.pause()
        self.instance._notify(generation, 'error', 'stale')
        eventually(lambda: not self.instance._workers)
        self.assertEqual(self.records, [])
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
