"""Persistent, private audiobook media storage; no NVDA dependencies."""
from contextlib import ExitStack
from copy import deepcopy
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from threading import RLock
import uuid
import zipfile

AUDIO_EXTENSIONS = frozenset(".mp3 .m4a .m4b .ogg .opus .flac .wav .aac .wma".split())
MAX_ENTRIES = 10000
MAX_JSON_BYTES = 16 * 1024 ** 2
MAX_FILE_BYTES = 8 * 1024 ** 3
MAX_TOTAL_BYTES = 32 * 1024 ** 3
MIN_FREE_BYTES = 64 * 1024 ** 2
COPY_CHUNK_SIZE = 1024 * 1024


class ImportCancelled(Exception):
    """Import was explicitly cancelled, leaving no new book or media."""


def _cancelled(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise ImportCancelled("Importowanie zostało anulowane.")


def _check_size(size, total):
    if size < 0 or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
        raise LibraryError("Źródło przekracza limit rozmiaru pliku lub całej książki.")



def _natural_key(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"([0-9]+)", str(value)))


class LibraryError(Exception):
    """An actionable library error suitable for presentation to the user."""


class FolderSelectionRequired(LibraryError):
    """The user must explicitly choose a folder containing direct recordings."""


def _valid_duration(value):
    try:
        return type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        return False


def _valid_signature(value):
    return (isinstance(value, dict) and type(value.get('size')) is int
            and value['size'] >= 0 and type(value.get('mtime_ns')) is int)


def _signature(path):
    info = _check_path(path).stat()
    if not stat.S_ISREG(info.st_mode):
        raise LibraryError('Plik dźwiękowy nie jest zwykłym plikiem.')
    # Copy + directory rename changes inode/ctime on some filesystems. Only
    # the durable size/mtime pair identifies the copied media after restart.
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def _measure_duration(path, probe, cancel=None):
    _cancelled(cancel)
    before = _signature(path)
    if not callable(probe):
        raise LibraryError('Nie skonfigurowano ffprobe do pomiaru długości nagrań.')
    try:
        duration = probe(path)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, subprocess.SubprocessError) as error:
        raise LibraryError(f'Nie można ustalić długości nagrania {path.name}: {error}') from error
    _cancelled(cancel)
    if not _valid_duration(duration):
        raise LibraryError(f'Nieprawidłowa długość nagrania: {path.name}')
    if _signature(path) != before:
        raise LibraryError(f'Plik zmienił się podczas pomiaru długości: {path.name}')
    return {'duration': float(duration), 'signature': before}


def _filesystem_errors(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, EOFError, struct.error) as error:
            raise LibraryError(f"Błąd dostępu do plików biblioteki: {error}") from error
    return checked


def _check_path(path):
    """Reject symlinks and Windows junction/reparse points in all ancestors."""
    path = Path(path).absolute()
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LibraryError(f"Niedozwolone dowiązanie lub punkt połączenia: {part}")
    return path


def _directory_files(source, cancel=None):
    """Read only immediate audio files, never descend into another folder."""
    files = []
    total = subfolders = 0
    source = _check_path(source)
    for count, path in enumerate(source.iterdir(), 1):
        _cancelled(cancel)
        if count > MAX_ENTRIES:
            raise LibraryError("Przekroczono limit liczby plików i folderów.")
        _check_path(path)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            subfolders += 1
        elif not stat.S_ISREG(info.st_mode):
            raise LibraryError(f"Niedozwolony typ pliku: {path}")
        elif path.suffix.lower() in AUDIO_EXTENSIONS:
            total += info.st_size
            _check_size(info.st_size, total)
            files.append(path)
    if not files and subfolders:
        noun = 'podfolder' if subfolders == 1 else 'podfoldery'
        raise FolderSelectionRequired(f'Folder zawiera {noun}. Otwórz go i zaznacz folder z nagraniami.')
    return sorted(files, key=lambda p: _natural_key(p.name))


def _safe_relative(name):
    if not isinstance(name, str) or not name or name.startswith("/"):
        raise LibraryError("Niebezpieczna ścieżka pliku.")
    if "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        raise LibraryError(f"Niebezpieczna ścieżka pliku: {name!r}")
    parts = name.split("/")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((".", " "))
                or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I)):
            raise LibraryError(f"Niebezpieczna ścieżka pliku: {name!r}")
    return name


def _zip_preflight(stream):
    """Bound central-directory allocation before constructing ZipFile (ZIP64 too)."""
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    if length > MAX_TOTAL_BYTES + 16 * 1024 ** 2:
        raise LibraryError("Archiwum ZIP przekracza limit rozmiaru.")
    stream.seek(max(0, length - 65557))
    tail = stream.read(65557)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        raise LibraryError("Nieprawidłowe lub ucięte archiwum ZIP.")
    _, disk, directory_disk, disk_count, count, size, offset, comment = struct.unpack_from("<4s4H2LH", tail, index)
    if disk or directory_disk or disk_count != count or index + 22 + comment != len(tail):
        raise LibraryError("Nieprawidłowe lub wieloczęściowe archiwum ZIP.")
    end_position = length - len(tail) + index
    locator = b""
    if end_position >= 20:
        stream.seek(end_position - 20)
        locator = stream.read(20)
    if (locator.startswith(b"PK\x06\x07") or count == 65535
            or size == 0xffffffff or offset == 0xffffffff):
        if end_position < 20:
            raise LibraryError("Nieprawidłowe archiwum ZIP64.")
        stream.seek(end_position - 20)
        signature, disk, record_offset, disks = struct.unpack("<4sLQL", stream.read(20))
        if signature != b"PK\x06\x07" or disk or disks != 1 or record_offset != end_position - 76:
            raise LibraryError("Nieprawidłowe lub wieloczęściowe archiwum ZIP64.")
        stream.seek(record_offset)
        record = stream.read(56)
        if len(record) != 56:
            raise LibraryError("Ucięte archiwum ZIP64.")
        signature, record_size, _, _, disk, directory_disk, disk_count, count, size, offset = struct.unpack("<4sQ2H2L4Q", record)
        if signature != b"PK\x06\x06" or record_size != 44 or disk or directory_disk or disk_count != count:
            raise LibraryError("Nieprawidłowe archiwum ZIP64.")
    if count > MAX_ENTRIES or size > 16 * 1024 ** 2 or offset + size > length:
        raise LibraryError("Archiwum ZIP przekracza limit liczby wpisów lub rozmiaru metadanych.")
    stream.seek(0)


def _zip_files(archive, cancel=None):
    files = []
    seen = set()
    total = 0
    if len(archive.infolist()) > MAX_ENTRIES:
        raise LibraryError("Archiwum przekracza limit liczby wpisów ZIP.")
    for entry in archive.infolist():
        _cancelled(cancel)
        total += entry.file_size
        _check_size(entry.file_size, total)
        name = entry.orig_filename
        _safe_relative(name[:-1] if name.endswith("/") else name)
        mode = entry.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if (kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or (entry.external_attr & 0x400) or entry.flag_bits & 1):
            raise LibraryError(f"Niedozwolony lub zaszyfrowany wpis ZIP: {name}")
        if name.casefold() in seen:
            raise LibraryError(f"Powtórzona ścieżka ZIP: {name}")
        seen.add(name.casefold())
        if not entry.is_dir() and Path(name).suffix.lower() in AUDIO_EXTENSIONS:
            files.append(entry)
    return sorted(files, key=lambda entry: _natural_key(entry.filename))


def scan_candidates(folder):
    """Return immediate archives and, first, the folder if it has direct audio.

    Never traverse child folders or offer them as books on the user's behalf.
    """
    try:
        folder = _check_path(folder)
        if not folder.is_dir():
            raise LibraryError("Wybierz istniejący folder do przeszukania.")
        audio = _directory_files(folder)
        candidates = [path for path in folder.iterdir()
                      if path.is_file() and path.suffix.lower() in (".zip", ".rar", ".7z")]
        candidates.sort(key=lambda path: _natural_key(path.name))
        if audio:
            candidates.insert(0, folder)
        return candidates
    except OSError as error:
        raise LibraryError(f"Nie można przeszukać folderu: {error}") from error


class Library:
    @_filesystem_errors
    def __init__(self, addons_dir, *, media_dir=None, duration_probe=None, sevenzip_path=None):
        # A configured ffprobe callable is mandatory when importing/probing.
        # Opening a library never scans or migrates existing media.
        self._duration_probe = duration_probe
        self._sevenzip_path = sevenzip_path
        self.addons_dir = Path(addons_dir).absolute()
        self.media_dir = (Path(media_dir).absolute() if media_dir is not None
                          else self.addons_dir / "czytelnia-biblioteka")
        self.json_path = self.addons_dir / "czytelnia_config.json"
        self._lock = RLock()
        _check_path(self.addons_dir)
        _check_path(self.media_dir)
        self._snapshot = self._read_json_bytes()
        self._books = self._decode(self._snapshot) if self._snapshot is not None else []

    def _read_json_bytes(self):
        _check_path(self.json_path)
        try:
            with self.json_path.open("rb") as stream:
                data = stream.read(MAX_JSON_BYTES + 1)
            if len(data) > MAX_JSON_BYTES:
                raise LibraryError("Plik czytelnia_config.json przekracza dozwolony rozmiar.")
            return data
        except FileNotFoundError:
            return None
        except OSError as error:
            raise LibraryError(f"Nie można odczytać czytelnia_config.json: {error}") from error

    @classmethod
    def _decode(cls, data):
        try:
            document = json.loads(data)
            if (not isinstance(document, dict) or type(document.get("version")) is not int
                    or document["version"] != 1 or not isinstance(document.get("books"), list)):
                raise ValueError("nieprawidłowy format lub wersja")
            seen = set()
            for book in document["books"]:
                if (not isinstance(book, dict) or not isinstance(book.get("id"), str)
                        or not re.fullmatch(r"[0-9a-f]{32}", book["id"])
                        or book["id"] in seen or not isinstance(book.get("title"), str)
                        or not isinstance(book.get("tracks"), list) or not book["tracks"]
                        or not isinstance(book.get("source", ""), str)):
                    raise ValueError("nieprawidłowy opis książki")
                seen.add(book["id"])
                paths = set()
                for track in book["tracks"]:
                    if not isinstance(track, dict) or not isinstance(track.get("title"), str):
                        raise ValueError("nieprawidłowy opis ścieżki")
                    relative = _safe_relative(track["path"])
                    if relative.casefold() in paths or Path(relative).suffix.lower() not in AUDIO_EXTENSIONS:
                        raise ValueError("nieprawidłowa lub powtórzona ścieżka")
                    paths.add(relative.casefold())
                    if 'duration' in track or 'signature' in track:
                        if (not _valid_duration(track.get('duration'))
                                or not _valid_signature(track.get('signature'))):
                            raise ValueError('nieprawidłowa długość lub sygnatura nagrania')
                cls._validate_position(book, book["track_index"], book["position"])
                # Missing legacy metadata means ordinary numbering; do not migrate.
                cls._validate_position(book, book.get('numbering_start', 0), 0)
                if 'bookmark_return' in book:
                    point = book['bookmark_return']
                    if (not isinstance(point, dict)
                            or set(point) != {'track_index', 'position'}):
                        raise ValueError('nieprawidłowe miejsce powrotu z zakładki')
                    cls._validate_position(book, point['track_index'], point['position'])
                bookmarks = book.get('bookmarks', [])
                if not isinstance(bookmarks, list):
                    raise ValueError('nieprawidłowa lista zakładek')
                bookmark_ids = set()
                for bookmark in bookmarks:
                    if (not isinstance(bookmark, dict)
                            or not isinstance(bookmark.get('id'), str)
                            or not re.fullmatch(r'[0-9a-f]{32}', bookmark['id'])
                            or bookmark['id'] in bookmark_ids
                            or not isinstance(bookmark.get('name'), str)
                            or not bookmark['name']
                            or bookmark['name'] != bookmark['name'].strip()):
                        raise ValueError('nieprawidłowy opis zakładki')
                    bookmark_ids.add(bookmark['id'])
                    cls._validate_position(book, bookmark['track_index'], bookmark['position'])
                number = book.get('next_bookmark_number', len(bookmarks) + 1)
                if type(number) is not int or number <= len(bookmarks):
                    raise ValueError('nieprawidłowy licznik zakładek')
            return document["books"]
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, LibraryError) as error:
            raise LibraryError(f"Uszkodzony plik czytelnia_config.json; nie został zmieniony. {error}") from error

    def books(self):
        with self._lock:
            return deepcopy(self._books)

    @_filesystem_errors
    def move_book(self, book_id, direction):
        """Persist only list order; book identities and playback data stay intact."""
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Nieprawidłowy kierunek przesunięcia książki.')
        with self._lock:
            book = self._book(book_id)
            index = next(i for i, item in enumerate(self._books) if item['id'] == book_id)
            target = index + direction
            if not 0 <= target < len(self._books):
                return None
            updated = deepcopy(self._books)
            updated[index], updated[target] = updated[target], updated[index]
            self._write(updated)
            self._books = updated
            return book['title'], target + 1, len(updated)

    def _book(self, book_id):
        if not isinstance(book_id, str) or not re.fullmatch(r"[0-9a-f]{32}", book_id):
            raise LibraryError("Nieprawidłowy identyfikator książki.")
        for book in self._books:
            if book["id"] == book_id:
                return book
        raise LibraryError("Nie znaleziono książki w bibliotece.")

    def get_book(self, book_id):
        with self._lock:
            return deepcopy(self._book(book_id))

    @_filesystem_errors
    def set_numbering_start(self, book_id, index):
        """Replace only the display-numbering origin, using a durable copy-on-write."""
        with self._lock:
            book = self._book(book_id)
            self._validate_position(book, index, 0)
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book_id)
            target['numbering_start'] = index
            self._write(updated)
            self._books = updated

    def bookmarks(self, book_id):
        """Return detached bookmarks ordered by track/time, never by their names."""
        with self._lock:
            return deepcopy(sorted(self._book(book_id).get('bookmarks', []),
                                   key=lambda item: (item['track_index'], item['position'])))

    def get_bookmark_return(self, book_id):
        """Detached first-excursion point, absent in legacy books without a write."""
        with self._lock:
            return deepcopy(self._book(book_id).get('bookmark_return'))

    @_filesystem_errors
    def save_bookmark_return(self, book_id, track_index, position):
        """Persist only the first point; later bookmark jumps must not replace it."""
        with self._lock:
            book = self._book(book_id)
            self._validate_position(book, track_index, position)
            if 'bookmark_return' in book:
                return deepcopy(book['bookmark_return'])
            point = dict(track_index=track_index, position=float(position))
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book_id)
            target['bookmark_return'] = point
            self._write(updated)
            self._books = updated
            return deepcopy(point)

    @_filesystem_errors
    def complete_bookmark_return(self, book_id):
        """Restore ordinary progress and consume the anchor in one durable commit."""
        with self._lock:
            book = self._book(book_id)
            if 'bookmark_return' not in book:
                return
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book_id)
            point = target.pop('bookmark_return')
            target.update(point)
            self._write(updated)
            self._books = updated

    @_filesystem_errors
    def add_bookmark(self, book_id, track_index, position):
        with self._lock:
            book = self._book(book_id)
            self._validate_position(book, track_index, position)
            number = book.get('next_bookmark_number', len(book.get('bookmarks', [])) + 1)
            bookmark = dict(id=uuid.uuid4().hex, name='Zakładka',
                            track_index=track_index, position=float(position))
            if any(item['id'] == bookmark['id'] for item in book.get('bookmarks', [])):
                raise LibraryError('Kolizja identyfikatora zakładki. Spróbuj ponownie.')
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book_id)
            target.setdefault('bookmarks', []).append(bookmark)
            target['next_bookmark_number'] = number + 1
            self._write(updated)
            self._books = updated
            return deepcopy(bookmark)

    @staticmethod
    def _bookmark(book, bookmark_id):
        if not isinstance(bookmark_id, str) or not re.fullmatch(r'[0-9a-f]{32}', bookmark_id):
            raise LibraryError('Nieprawidłowy identyfikator zakładki.')
        for bookmark in book.get('bookmarks', []):
            if bookmark['id'] == bookmark_id:
                return bookmark
        raise LibraryError('Nie znaleziono zakładki w książce.')

    def get_bookmark(self, book_id, bookmark_id):
        with self._lock:
            return deepcopy(self._bookmark(self._book(book_id), bookmark_id))

    @_filesystem_errors
    def rename_bookmark(self, book_id, bookmark_id, name):
        if not isinstance(name, str) or not name.strip():
            raise LibraryError('Nazwa zakładki nie może być pusta.')
        with self._lock:
            self._bookmark(self._book(book_id), bookmark_id)
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book_id)
            bookmark = self._bookmark(target, bookmark_id)
            bookmark['name'] = name.strip()
            self._write(updated)
            self._books = updated
            return deepcopy(bookmark)

    @_filesystem_errors
    def remove_bookmark(self, book_id, bookmark_id):
        with self._lock:
            self._bookmark(self._book(book_id), bookmark_id)
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book_id)
            target['bookmarks'] = [item for item in target['bookmarks'] if item['id'] != bookmark_id]
            self._write(updated)
            self._books = updated

    @staticmethod
    def _validate_position(book, track_index, seconds):
        if type(track_index) is not int or not 0 <= track_index < len(book["tracks"]):
            raise LibraryError("Nieprawidłowy numer ścieżki.")
        try:
            valid = type(seconds) in (int, float) and seconds >= 0 and math.isfinite(seconds)
        except OverflowError:
            valid = False
        if not valid:
            raise LibraryError("Pozycja odtwarzania musi być nieujemną, skończoną liczbą.")

    @_filesystem_errors
    def save_position(self, book_id, track_index, seconds):
        with self._lock:
            book = self._book(book_id)
            self._validate_position(book, track_index, seconds)
            updated = deepcopy(self._books)
            target = next(item for item in updated if item["id"] == book_id)
            target.update(track_index=track_index, position=float(seconds))
            self._write(updated)
            self._books = updated

    @_filesystem_errors
    def track_path(self, book_id, track_index):
        with self._lock:
            book = self._book(book_id)
            self._validate_position(book, track_index, 0)
            relative = _safe_relative(book["tracks"][track_index]["path"])
            path = _check_path(self.media_dir / book_id / relative)
            if not path.is_file():
                raise LibraryError(f"Brak pliku dźwiękowego: {path.name}")
            return path

    @_filesystem_errors
    def track_duration(self, book_id, track_index):
        """Read the imported duration; remeasure and persist only changed media."""
        with self._lock:
            book = self._book(book_id)
            self._validate_position(book, track_index, 0)
            track = book['tracks'][track_index]
            if 'duration' not in track:
                raise LibraryError('Brak zapisanej długości. Zaimportuj książkę ponownie.')
            path = self.track_path(book_id, track_index)
            if track['signature'] == _signature(path):
                return track['duration']
            metadata = _measure_duration(path, self._duration_probe)
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book_id)
            target['tracks'][track_index].update(metadata)
            self._write(updated)
            self._books = updated
            return metadata['duration']

    def duration_snapshot(self, book_id):
        """Detached values + stateless probe callable for a background worker."""
        with self._lock:
            book = self._book(book_id)
            tracks = []
            for track in book['tracks']:
                tracks.append(dict(path=self.media_dir / book_id / _safe_relative(track['path']),
                                   duration=track.get('duration'),
                                   signature=deepcopy(track.get('signature'))))
            return dict(book_id=book_id, tracks=tracks, probe=self._duration_probe)

    @staticmethod
    def probe_durations(snapshot, cancel=None):
        """No live library/player or lock. Missing metadata is NOT backfilled."""
        results = [None] * len(snapshot['tracks'])
        for index, track in enumerate(snapshot['tracks']):
            try:
                _cancelled(cancel)
                if (not _valid_duration(track['duration'])
                        or not _valid_signature(track['signature'])):
                    continue
                if _signature(track['path']) == track['signature']:
                    results[index] = dict(duration=track['duration'], signature=track['signature'])
                else:
                    results[index] = _measure_duration(track['path'], snapshot['probe'], cancel)
            except ImportCancelled:
                break
            except (OSError, LibraryError):
                # Missing/broken/changed media has unknown duration, never zero.
                continue
        return results

    @_filesystem_errors
    def commit_durations(self, snapshot, results):
        """Revalidate and atomically merge probe results without losing position."""
        with self._lock:
            book = self._book(snapshot['book_id'])
            if len(results) != len(book['tracks']) or len(snapshot['tracks']) != len(results):
                raise LibraryError('Nieaktualny wynik pomiaru długości książki.')
            updated = deepcopy(self._books)
            target = next(item for item in updated if item['id'] == book['id'])
            durations = [None] * len(results)
            changed = False
            for index, (original, result) in enumerate(zip(snapshot['tracks'], results)):
                if result is None:
                    continue
                try:
                    signature = _signature(self.track_path(book['id'], index))
                except (OSError, LibraryError):
                    continue
                if (not _valid_duration(result.get('duration'))
                        or not _valid_signature(result.get('signature'))
                        or signature != result['signature']):
                    continue
                track = target['tracks'][index]
                if (track.get('duration') == original['duration']
                        and track.get('signature') == original['signature']):
                    if track['duration'] != result['duration'] or track['signature'] != signature:
                        track.update(duration=result['duration'], signature=signature)
                        changed = True
                    durations[index] = result['duration']
                elif track.get('signature') == signature and _valid_duration(track.get('duration')):
                    # Playback may already have committed a newer valid result.
                    durations[index] = track['duration']
            if changed:
                self._write(updated)
                self._books = updated
            return durations

    @_filesystem_errors
    def remove_book(self, book_id):
        """Delete only our UUID directory, never a path supplied by JSON."""
        with self._lock:
            self._book(book_id)
            owned = _check_path(self.media_dir / book_id)
            tombstone = self.media_dir / (".remove-" + uuid.uuid4().hex)
            moved = False
            try:
                if owned.exists():
                    if not owned.is_dir():
                        raise LibraryError("Folder książki został zastąpiony innym plikiem.")
                    _directory_files(owned)  # Inspect *all* entries before deleting any.
                    owned.rename(tombstone)
                    moved = True
                updated = [book for book in self._books if book["id"] != book_id]
                try:
                    self._write(updated)
                except Exception:
                    if moved:
                        tombstone.rename(owned)
                    raise
                self._books = updated
                if moved:
                    shutil.rmtree(tombstone)
            except OSError as error:
                raise LibraryError(f"Nie można usunąć plików książki: {error}") from error

    def _write(self, books):
        name = None
        try:
            _check_path(self.addons_dir)
            self.addons_dir.mkdir(parents=True, exist_ok=True)
            if self._read_json_bytes() != self._snapshot:
                raise LibraryError("Plik czytelnia_config.json zmienił się poza biblioteką. Zamknij i otwórz bibliotekę ponownie; plik nie został nadpisany.")
            data = json.dumps({"version": 1, "books": books}, ensure_ascii=False,
                              indent=2, allow_nan=False).encode("utf-8")
            if len(data) > MAX_JSON_BYTES:
                raise LibraryError("Biblioteka przekracza dozwolony rozmiar pliku czytelnia_config.json.")
            fd, name = tempfile.mkstemp(prefix=".czytelnia-", suffix=".tmp", dir=self.addons_dir)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _check_path(self.json_path)
            os.replace(name, self.json_path)
            self._snapshot = data
        except (OSError, ValueError) as error:
            raise LibraryError(f"Nie można zapisać biblioteki: {error}") from error
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    @_filesystem_errors
    def import_book(self, source, progress=None, cancel=None, *, stage_progress=None):
        """Copy a directory or ZIP/RAR/7z. Callbacks run on the caller's thread.

        ``progress(done_bytes, total_bytes)`` retains its copy-only contract.
        ``stage_progress(stage, done, total)`` counts work units, NOT time:
        one copy unit (fractional by bytes) and one duration-probe unit per
        track, plus one atomic library commit. Only 'complete' reaches 100%.
        Stages are prepare/copy/probe/save/complete; preparation has 0/1 until
        the file count is known. Never call blocking GUI methods here.
        cancel is a predicate or Event. Import holds the instance's RLock.
        """
        def report(stage, done, total):
            if stage_progress:
                stage_progress(stage, done, total)

        with self._lock, ExitStack() as stack:
            _cancelled(cancel)
            report('prepare', 0, 1)
            try:
                source = _check_path(source).resolve()
                media = self.media_dir.resolve()
                if source == media or media in source.parents or source in media.parents:
                    raise LibraryError("Nie można importować folderu biblioteki ani folderu zawierającego bibliotekę.")
                archive = None
                if source.is_dir():
                    files = _directory_files(source, cancel)
                elif source.is_file() and source.suffix.lower() == ".zip":
                    stream = stack.enter_context(source.open("rb"))
                    _zip_preflight(stream)
                    archive = stack.enter_context(zipfile.ZipFile(stream))
                    files = _zip_files(archive, cancel)
                elif source.is_file() and source.suffix.lower() in ('.rar', '.7z'):
                    # Normal plugin import stays relative. Standalone stdlib tests
                    # load this file without a package; load only its trusted sibling,
                    # without changing sys.path or the process-wide module registry.
                    if __package__:
                        from . import sevenzip
                    else:
                        import importlib.util
                        spec = importlib.util.spec_from_file_location(
                            '_czytelnia_sevenzip', Path(__file__).with_name('sevenzip.py'))
                        sevenzip = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(sevenzip)
                    archive = sevenzip.Archive(source, self._sevenzip_path,
                        error=LibraryError, check_cancel=lambda: _cancelled(cancel),
                        safe_relative=_safe_relative, max_entries=MAX_ENTRIES,
                        max_file=MAX_FILE_BYTES, max_total=MAX_TOTAL_BYTES)
                    files = sorted((entry for entry in archive.entries
                                    if Path(entry.filename).suffix.lower() in AUDIO_EXTENSIONS),
                                   key=lambda entry: _natural_key(entry.filename))
                else:
                    raise LibraryError("Wybierz istniejący folder lub archiwum ZIP, RAR albo 7z.")
            except (OSError, zipfile.BadZipFile) as error:
                raise LibraryError(f"Nie można odczytać źródła: {error}") from error
            if not files:
                raise LibraryError("Nie znaleziono plików dźwiękowych w źródle.")
            for existing in self._books:
                if os.path.normcase(existing.get("source", "")) == os.path.normcase(str(source)):
                    if all(_check_path(self.media_dir / existing["id"] / t["path"]).is_file()
                           for t in existing["tracks"]):
                        report('complete', 1, 1)
                        return deepcopy(existing)
                    raise LibraryError("Książka jest już zapisana, ale brakuje jej plików. Usuń wpis przed ponownym importem.")
            _check_path(self.media_dir)
            self.media_dir.mkdir(parents=True, exist_ok=True)
            sizes = [file.file_size if archive else file.stat().st_size for file in files]
            total_bytes = sum(sizes)
            total_units = 2 * len(files) + 1
            if shutil.disk_usage(self.media_dir).free < total_bytes + MIN_FREE_BYTES:
                raise LibraryError("Za mało wolnego miejsca na dysku do importowania książki.")
            book_id = uuid.uuid4().hex
            destination = _check_path(self.media_dir / book_id)
            if destination.exists() or any(book["id"] == book_id for book in self._books):
                raise LibraryError("Kolizja identyfikatora książki. Spróbuj ponownie.")
            staging = Path(tempfile.mkdtemp(prefix=".import-", dir=self.media_dir))
            committed = moved = False
            try:
                tracks = []
                done = 0
                if progress:
                    progress(0, total_bytes)
                for index, file in enumerate(files):
                    _cancelled(cancel)
                    report('copy', 2 * index, total_units)
                    original = Path(file.filename) if archive else file
                    name = f"{index:06d}{original.suffix.lower()}"
                    if not archive:
                        _check_path(file)
                    with (archive.open(file) if archive else file.open("rb")) as reader:
                        with (staging / name).open("xb") as writer:
                            copied = 0
                            while True:
                                _cancelled(cancel)
                                chunk = reader.read(COPY_CHUNK_SIZE)
                                if not chunk:
                                    break
                                copied += len(chunk)
                                if copied > sizes[index]:
                                    raise LibraryError("Rozmiar pliku zmienił się podczas importu.")
                                if shutil.disk_usage(self.media_dir).free < len(chunk) + MIN_FREE_BYTES:
                                    raise LibraryError("Za mało wolnego miejsca podczas importowania książki.")
                                writer.write(chunk)
                                done += len(chunk)
                                report('copy', 2 * index + copied / sizes[index], total_units)
                                if progress:
                                    progress(done, total_bytes)
                            if copied != sizes[index]:
                                raise LibraryError("Plik został skrócony podczas importu.")
                            writer.flush()
                            os.fsync(writer.fileno())
                    report('probe', 2 * index + 1, total_units)
                    metadata = _measure_duration(staging / name, self._duration_probe, cancel)
                    report('probe', 2 * index + 2, total_units)
                    tracks.append({"path": name, "title": original.stem, **metadata})
                if archive is not None and source.suffix.lower() in ('.rar', '.7z'):
                    archive.unchanged()
                book = {"id": book_id, "title": source.stem if archive else source.name, "tracks": tracks,
                        "track_index": 0, "position": 0.0, "source": str(source),
                        "numbering_start": 0}
                _cancelled(cancel)
                if destination.exists():
                    raise LibraryError("Folder docelowy już istnieje; import przerwany.")
                report('save', total_units - 1, total_units)
                staging.rename(destination)
                moved = True
                updated = self._books + [book]
                self._write(updated)
                self._books = updated
                committed = True
                report('complete', total_units, total_units)
                return deepcopy(book)
            except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
                raise LibraryError(f"Nie można zaimportować książki: {error}") from error
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
                if moved and not committed and destination.exists():
                    shutil.rmtree(destination)
