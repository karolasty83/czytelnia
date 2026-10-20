"""Private, stdout-only 7-Zip adapter. Never extract archive paths to disk."""
from collections import namedtuple
import os
from pathlib import Path
import queue
import re
import unicodedata
import subprocess
import threading
import time

Entry = namedtuple('Entry', 'filename file_size member')
LIST_LIMIT = 8 * 1024 * 1024
PROCESS_TIMEOUT = 300.0
IMPORT_TIMEOUT = 1800.0


class _Reader:
    """Drain both pipes; bounded queue, cancellation even during a stalled read."""
    def __init__(self, owner, arguments):
        self.owner = owner
        self.queue = queue.Queue(2)
        self.stop = threading.Event()
        self.errors = bytearray()
        self.closed = False
        self.deadline = min(time.monotonic() + PROCESS_TIMEOUT, owner.deadline)
        self.process = subprocess.Popen(
            [str(owner.executable), *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.threads = [threading.Thread(target=self._stdout, name='czytelnia-7z-stdout'),
                        threading.Thread(target=self._stderr, name='czytelnia-7z-stderr')]
        for thread in self.threads:
            thread.start()

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.queue.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def _stdout(self):
        try:
            while not self.stop.is_set():
                data = self.process.stdout.read(65536)
                if not data:
                    break
                self._put(data)
        except OSError as error:
            self._put(error)
        finally:
            self._put(None)

    def _stderr(self):
        try:
            while not self.stop.is_set():
                data = self.process.stderr.read(65536)
                if not data:
                    break
                self.errors.extend(data[:max(0, 16384 - len(self.errors))])
        except OSError:
            pass

    def _check(self):
        self.owner.check_cancel()
        if time.monotonic() >= self.deadline:
            raise self.owner.error('Przekroczono czas operacji 7-Zip.')

    def read(self, size=-1):
        try:
            while True:
                self._check()
                try:
                    value = self.queue.get(timeout=.05)
                    break
                except queue.Empty:
                    continue
            if isinstance(value, Exception):
                raise self.owner.error('Nie można odczytać wyjścia 7-Zip.') from value
            if value is None:
                while self.process.poll() is None:
                    self._check()
                    time.sleep(.01)
                if self.process.returncode != 0:
                    raise self.owner.error('7-Zip odrzucił archiwum (uszkodzone, zaszyfrowane lub nieobsługiwane).')
                self.owner.unchanged()
                return b''
            return value
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        for thread in self.threads:
            thread.join()
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Archive:
    def __init__(self, source, executable, *, error, check_cancel, safe_relative,
                 max_entries, max_file, max_total):
        self.error, self.check_cancel = error, check_cancel
        self.source = Path(source)
        self.executable = Path(executable) if executable is not None else None
        if self.executable is None or not self.executable.is_file():
            raise error('Brak narzędzia 7-Zip do importowania archiwów RAR i 7z. Zainstaluj ponownie dodatek.')
        self.deadline = time.monotonic() + IMPORT_TIMEOUT
        self.snapshot = self._state()
        if self.snapshot[2] > max_total:
            raise error('Archiwum przekracza limit rozmiaru.')
        self.switches = ['-sccUTF-8', '-scsUTF-8', '-spd', '-y', '-pCzytelniaNoPassword']
        data = bytearray()
        with _Reader(self, ['l', '-slt', *self.switches, '--', str(source)]) as reader:
            while True:
                chunk = reader.read()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > LIST_LIMIT:
                    raise error('Archiwum przekracza limit rozmiaru metadanych.')
        try:
            text = data.decode('utf-8', 'strict').replace('\r\n', '\n')
            prefix, body = text.split('\n----------\n')
            _, header = prefix.split('\n--\n')
            # 7-Zip prints the optional free-form archive comment last in
            # the archive header, never as file metadata. Do not parse its text.
            header = header.partition('\nComment = ')[0]
            metadata = self._fields(header.strip('\n'))
            if metadata.get('Type') not in ('7z', 'Rar', 'Rar5'):
                raise ValueError('Nieobsługiwany typ archiwum')
            self._flags(metadata)
            self.entries = []
            blocks = body.strip('\n').split('\n\n') if body.strip('\n') else []
            if len(blocks) > max_entries:
                raise ValueError('Przekroczono limit liczby wpisów')
            total = 0
            seen = set()
            for block in blocks:
                check_cancel()
                values = self._fields(block)
                self._flags(values)
                member = values['Path']
                if any(unicodedata.category(c).startswith('C') for c in member):
                    raise ValueError('Znaki sterujące w nazwie')
                name = member.replace('\\', '/')
                safe_relative(name)
                if name.casefold() in seen:
                    raise ValueError('Powtórzona ścieżka')
                seen.add(name.casefold())
                if not re.fullmatch(r'[0-9]{1,20}', values['Size']):
                    raise ValueError('Nieprawidłowy rozmiar')
                size = int(values['Size'])
                total += size
                if size > max_file or total > max_total:
                    raise ValueError('Przekroczono limit rozmiaru pliku lub książki')
                attributes = values.get('Attributes', '')
                # 7-Zip renders DOS flags and optionally a POSIX permission string.
                if not re.fullmatch(r'[DRAHSCNOTI_V .]*(?: [d-][rwxstST-]{9})?', attributes):
                    raise ValueError('Niedozwolony typ lub atrybuty wpisu')
                directory = 'D' in attributes or values.get('Folder') == '+'
                if not directory:
                    self.entries.append(Entry(name, size, member))
        except (ValueError, KeyError, UnicodeError) as exc:
            raise error(f'Niebezpieczne lub nieobsługiwane metadane archiwum: {exc}.') from exc

    @staticmethod
    def _fields(block):
        fields = {}
        for line in block.split('\n'):
            key, value = line.split(' = ', 1)
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9 ]*', key) or key in fields:
                raise ValueError('Powtórzone lub niejednoznaczne pole metadanych')
            if any(unicodedata.category(c).startswith('C') for c in value):
                raise ValueError('Znaki sterujące w metadanych')
            fields[key] = value
        return fields

    @staticmethod
    def _flags(values):
        for key, value in values.items():
            lower = key.lower()
            if any(term in lower for term in ('link', 'reparse', 'stream')):
                # RAR5 lists these properties even for ordinary files. A link
                # named '-' is still a real link; only an empty target is absent.
                if key in ('Symbolic Link', 'Hard Link', 'Copy Link') and value == '':
                    continue
                if key == 'Alternate Stream' and value == '-':
                    continue
                raise ValueError('Niedozwolone dowiązanie lub strumień')
            if key == 'Mode' and not re.fullmatch(r'[d-][rwxstST-]{9}', value):
                raise ValueError('Niedozwolony typ wpisu')
            if key == 'Characteristics' and any(term in value.lower() for term in
                    ('link', 'reparse', 'stream', 'volume', 'encrypt')):
                raise ValueError('Niedozwolone właściwości archiwum')
            if key in ('Encrypted', 'Headers Encrypted') and value != '-':
                raise ValueError('Archiwum jest zaszyfrowane')
            if key in ('Split Before', 'Split After', 'Multivolume', 'Volume') and value not in ('-', '0'):
                raise ValueError('Archiwum wieloczęściowe')
            if key == 'Volumes' and value != '1' or key == 'Volume Index' and value not in ('', '0'):
                raise ValueError('Archiwum wieloczęściowe')


    def _state(self):
        info = self.source.stat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def unchanged(self):
        if self._state() != self.snapshot:
            raise self.error('Archiwum zmieniło się podczas importowania.')

    def open(self, entry):
        self.unchanged()
        return _Reader(self, ['x', '-so', *self.switches, '--', str(self.source), entry.member])
