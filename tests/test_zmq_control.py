"""ZMTP wire tests using a local fake REP; no third-party dependencies."""
import importlib.util
from pathlib import Path
import socket
import struct
import threading
import unittest
from unittest import mock
import time

PATH = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/zmq_control.py'
SPEC = importlib.util.spec_from_file_location('standalone_zmq_control', PATH)
control = importlib.util.module_from_spec(SPEC)
if PATH.exists():
    SPEC.loader.exec_module(control)

GREETING = b'\xff' + b'\0' * 8 + b'\x7f\x03\x00NULL' + b'\0' * 48
READY = b'\x05READY\x0bSocket-Type\0\0\0\x03REP'


def exact(sock, size):
    result = b''
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise EOFError()
        result += chunk
    return result


def frame(sock):
    flags = exact(sock, 1)[0]
    size = int.from_bytes(exact(sock, 8 if flags & 2 else 1), 'big')
    return flags, exact(sock, size)


class WireTests(unittest.TestCase):
    def test_initial_tcp_timeout_retries_within_total_deadline(self):
        client = control.VolumeControl()
        first, second = mock.Mock(), mock.Mock()
        first.connect.side_effect = TimeoutError('Windows loopback not ready')
        with mock.patch.object(control.socket, 'socket', side_effect=[first, second]), \
                mock.patch.object(client, '_send'), \
                mock.patch.object(client, '_read', return_value=GREETING), \
                mock.patch.object(client, '_frame', return_value=(4, READY)):
            client._connect(time.monotonic() + 0.5, lambda: True)
        first.close.assert_called_once()
        second.connect.assert_called_once_with(('127.0.0.1', client.port))
        client.close()

    def test_expired_connect_deadline_does_not_allocate_socket(self):
        client = control.VolumeControl()
        with mock.patch.object(control.socket, 'socket') as create:
            with self.assertRaises(TimeoutError):
                client._connect(time.monotonic() - 1, lambda: True)
            create.assert_not_called()
        client.close()

    def test_volume_and_timeout_bounds_reject_before_connect(self):
        client = control.VolumeControl()
        with mock.patch.object(client, '_connect') as connect:
            for value in (-1, 101, True, 37.0, None, '37', float('nan'), float('inf')):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    client.set_volume(value)
            for timeout in (0, -1, 11, True, None, '2', float('nan'), float('inf')):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    client.set_volume(37, timeout=timeout)
            connect.assert_not_called()
        client.close()

    def test_stale_closed_or_dead_process_never_connects(self):
        for closed in (False, True):
            client = control.VolumeControl()
            if closed:
                client.close()
            with mock.patch.object(client, '_connect') as connect:
                with self.assertRaises(control.ControlError):
                    client.set_volume(37, alive=lambda: False)
                connect.assert_not_called()
            self.assertTrue(client._closed)

    def test_request_timeout_is_total_bounded_and_closes_client(self):
        client = control.VolumeControl()
        client._socket, peer = socket.socketpair()
        started = time.monotonic()
        try:
            with self.assertRaises(control.ControlError):
                client.set_volume(37, timeout=0.05)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(client._closed)
            self.assertIsNone(client._socket)
            with self.assertRaises(control.ControlError):
                client.set_volume(37)
        finally:
            client.close()
            peer.close()

    def test_missing_endpoint_has_one_total_deadline(self):
        client = control.VolumeControl()
        started = time.monotonic()
        with self.assertRaises(control.ControlError):
            client.set_volume(0, timeout=0.07)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(client._closed)

    def test_malformed_frames_and_rejected_target_fail_closed(self):
        replies = {
            'reserved flags': b'\x80',
            'command more': b'\x05',
            'oversized': b'\x02' + struct.pack('!Q', 2 ** 63),
            'missing delimiter': b'\x00\x00',
            'nonempty delimiter': b'\x01\x01x',
            'multipart ack': b'\x01\x00\x01\x03' + b'0 x',
            'nonzero ack': b'\x01\x00\x00\x07' + b'38 Nope',
            'false zero': b'\x01\x00\x00\x03' + b'00 ',
            'bad utf8': b'\x01\x00\x00\x03' + b'0 \xff',
            'truncated': b'\x01\x00\x00\x10x',
        }
        for label, reply in replies.items():
            with self.subTest(label=label):
                client = control.VolumeControl()
                client._socket, peer = socket.socketpair()
                try:
                    peer.sendall(reply)
                    peer.shutdown(socket.SHUT_WR)
                    with self.assertRaises(control.ControlError):
                        client.set_volume(37, timeout=0.2)
                    self.assertTrue(client._closed)
                finally:
                    client.close()
                    peer.close()

    def test_long_reply_frame_is_supported(self):
        client = control.VolumeControl()
        client._socket, peer = socket.socketpair()
        reply = b'0 ' + b'x' * 300
        try:
            peer.sendall(b'\x01\x00\x02' + struct.pack('!Q', len(reply)) + reply)
            self.assertEqual(client.set_volume(37), reply.decode())
        finally:
            client.close()
            peer.close()

    def test_invalid_greetings_and_metadata_are_rejected(self):
        bad_greetings = [b'x' * 64, GREETING[:10] + b'\x02' + GREETING[11:],
                         GREETING[:12] + b'PLAIN' + b'\0' * 47]
        bad_ready = [b'\x05ERROR', b'\x05READY\0', b'\x05READY\x10x',
                     READY[:-1], READY.replace(b'REP', b'PUB'), READY + READY[6:],
                     b'\x05READY\x01x\xff\xff\xff\xff']
        for greeting, ready in [(g, READY) for g in bad_greetings] + [(GREETING, r) for r in bad_ready]:
            client = control.VolumeControl()
            peer = mock.Mock()
            with mock.patch.object(control.socket, 'socket', return_value=peer), \
                    mock.patch.object(client, '_read', return_value=greeting), \
                    mock.patch.object(client, '_frame', return_value=(4, ready)):
                with self.assertRaises(control.ControlError):
                    client.set_volume(37)
            self.assertTrue(client._closed)
            peer.close.assert_called_once()

    def test_req_null_handshake_and_volume_ack(self):
        self.assertTrue(hasattr(control, 'VolumeControl'), 'stdlib ZMTP client missing')
        client = control.VolumeControl()
        received, errors = [], []
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', client.port))
            listener.listen(1)
            listener.settimeout(2)
            def serve():
                try:
                    with listener.accept()[0] as peer:
                        peer.settimeout(2)
                        self.assertEqual(exact(peer, 64), GREETING)
                        # Deliberately fragmented greeting and command.
                        for part in (GREETING[:10], GREETING[10:], b'\x04' + bytes([len(READY)]) + READY):
                            peer.sendall(part)
                        flags, data = frame(peer)
                        self.assertEqual(flags, 4)
                        self.assertIn(b'Socket-Type\0\0\0\x03REQ', data)
                        for value in (0, 1, 37, 100):
                            self.assertEqual(frame(peer), (1, b''))
                            flags, data = frame(peer)
                            self.assertEqual(flags, 0)
                            received.append(data.decode())
                            peer.sendall(b'\x01\x00\x00\x09' + b'0 Success')
                except BaseException as error:
                    errors.append(error)
            worker = threading.Thread(target=serve)
            worker.start()
            try:
                for value in (0, 1, 37, 100):
                    self.assertEqual(client.set_volume(value), '0 Success')
            finally:
                client.close()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            if errors:
                raise errors[0]
        self.assertEqual(received, [f'{client.target} volume {value / 100:g}' for value in (0, 1, 37, 100)])
        self.assertRegex(client.target, r'^volume@bookvolume_[a-f0-9]{32}$')
        self.assertIn('127.0.0.1', client.filter(0))
        self.assertIn(client.target + '=0', client.filter(0))


if __name__ == '__main__':
    unittest.main()
