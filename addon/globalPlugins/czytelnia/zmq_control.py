"""Minimal bounded ZMTP 3.0 NULL/REQ client for ffplay's local azmq filter.

Wire references: https://rfc.zeromq.org/spec/23/ and /spec/28/.
Not a general ZeroMQ implementation: one loopback REP, one-part replies,
no reconnect after handshake or request failure, and no third-party imports.
NULL is not authentication. The random filter name prevents accidental control
of another player after a bind race, not attacks by other local processes.
"""
import math
import socket
import struct
import threading
import time
import uuid

TIMEOUT = 2.0
MAX_FRAME = 4096
_GREETING = b'\xff' + b'\0' * 8 + b'\x7f\x03\x00NULL' + b'\0' * 48
_READY = b'\x05READY\x0bSocket-Type\0\0\0\x03REQ'


class ControlError(RuntimeError):
    def __init__(self, message, *, diagnostic_stage=None, cause=None):
        super().__init__(message)
        self.diagnostic_stage = diagnostic_stage
        self.errno = getattr(cause, 'errno', None)
        self.winerror = getattr(cause, 'winerror', None)


def _volume(value):
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError('Volume must be an integer from 0 to 100')
    return value


class VolumeControl:
    def __init__(self):
        # libavfilter cannot inherit this bound socket. A bind race is therefore
        # possible; fail on startup/handshake/target errors, never scan ports.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind(('127.0.0.1', 0))
            self.port = reservation.getsockname()[1]
        self.target = 'volume@bookvolume_' + uuid.uuid4().hex
        self._socket = None
        self._closed = False
        self._lock = threading.RLock()

    def filter(self, value):
        # Quote for the filtergraph parser and escape colons for option parser.
        return "{}={:g},azmq=bind_address='tcp\\://127.0.0.1\\:{}'".format(
            self.target, _volume(value) / 100, self.port)

    def reset_connection(self):
        """Discard a graph's socket; lazily reconnect to the SAME target/port.

        Only the owner may call this after a primary demux/filtergraph reset.
        A permanently closed/failed controller cannot be revived.
        """
        with self._lock:
            if self._closed:
                raise ControlError('Volume control is closed')
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def close(self):
        with self._lock:
            self._closed = True
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('azmq deadline exceeded')
        return remaining

    def _send(self, data, deadline):
        self._socket.settimeout(self._remaining(deadline))
        self._socket.sendall(data)

    def _read(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            self._socket.settimeout(self._remaining(deadline))
            chunk = self._socket.recv(size - len(data))
            if not chunk:
                raise ControlError('azmq connection closed')
            data.extend(chunk)
        return bytes(data)

    def _frame(self, deadline):
        flags = self._read(1, deadline)[0]
        if flags & ~7 or flags & 4 and flags & 1:
            raise ControlError('Invalid ZMTP frame flags')
        size = int.from_bytes(self._read(8 if flags & 2 else 1, deadline), 'big')
        if size > MAX_FRAME:
            raise ControlError('Oversized ZMTP frame')
        return flags & ~2, self._read(size, deadline)

    def _connect(self, deadline, alive):
        while True:
            self._diagnostic_stage = 'peer_alive'
            if not alive():
                raise ControlError('ffplay exited before azmq became ready')
            self._diagnostic_stage = 'tcp_connect'
            remaining = self._remaining(deadline)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(min(0.1, remaining))
                sock.connect(('127.0.0.1', self.port))
            except (ConnectionRefusedError, TimeoutError):
                sock.close()
                time.sleep(min(0.02, self._remaining(deadline)))
                continue
            except BaseException:
                sock.close()
                raise
            self._socket = sock
            break
        self._diagnostic_stage = 'greeting_send'
        self._send(_GREETING, deadline)
        self._diagnostic_stage = 'greeting_receive'
        greeting = self._read(64, deadline)
        if (greeting[0] != 255 or greeting[9] != 127 or greeting[10] < 3
                or greeting[12:32] != b'NULL' + b'\0' * 16 or greeting[32] != 0):
            raise ControlError('Expected ZMTP 3 NULL greeting')
        self._diagnostic_stage = 'ready_send'
        self._send(b'\x04' + bytes([len(_READY)]) + _READY, deadline)
        self._diagnostic_stage = 'ready_receive'
        flags, ready = self._frame(deadline)
        if flags != 4 or not ready.startswith(b'\x05READY'):
            raise ControlError('Expected ZMTP READY')
        properties, offset = {}, 6
        while offset < len(ready):
            size = ready[offset]
            offset += 1
            if not size or offset + size + 4 > len(ready):
                raise ControlError('Malformed READY metadata')
            name = ready[offset:offset + size].lower()
            offset += size
            size = struct.unpack('!I', ready[offset:offset + 4])[0]
            offset += 4
            if offset + size > len(ready) or name in properties:
                raise ControlError('Malformed READY metadata')
            properties[name] = ready[offset:offset + size]
            offset += size
        if properties.get(b'socket-type') != b'REP':
            raise ControlError('Expected REP peer')

    def set_volume(self, value, timeout=TIMEOUT, alive=lambda: True):
        value = _volume(value)
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError('Timeout must be finite, positive and at most 10 seconds')
        with self._lock:
            if self._closed:
                raise ControlError('Volume control is closed')
            deadline = time.monotonic() + timeout
            try:
                self._diagnostic_stage = 'peer_alive'
                if not alive():
                    raise ControlError('ffplay is no longer running')
                if self._socket is None:
                    self._connect(deadline, alive)
                command = '{} volume {:g}'.format(self.target, value / 100).encode('ascii')
                self._diagnostic_stage = 'command_send'
                self._send(b'\x01\x00\x00' + bytes([len(command)]) + command, deadline)
                self._diagnostic_stage = 'reply_delimiter'
                if self._frame(deadline) != (1, b''):
                    raise ControlError('Expected REQ reply delimiter')
                self._diagnostic_stage = 'reply_body'
                flags, reply = self._frame(deadline)
                self._diagnostic_stage = 'reply_validate'
                if flags != 0:
                    raise ControlError('Expected final reply frame')
                text = reply.decode('utf-8', 'strict')
                if not text.startswith('0 '):
                    raise ControlError('ffplay rejected volume command: ' + text)
                self._diagnostic_stage = 'peer_alive'
                if not alive():
                    raise ControlError('ffplay exited during volume command')
                return text
            except (OSError, UnicodeError, ControlError) as error:
                self.close()
                raise ControlError('Live volume control failed: ' + str(error),
                                   diagnostic_stage=self._diagnostic_stage, cause=error) from error
