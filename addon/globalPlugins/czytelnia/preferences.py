"""NVDA-independent persistent preferences."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading


class PreferencesError(ValueError):
    """Invalid preferences or an unsafe storage path; original data is untouched."""


class Preferences:
    """Own JSON storage; the caller supplies an existing, trusted parent directory.

    Construction never creates directories or files. A missing document defaults
    to enabled; damaged/unsupported documents fail explicitly, never reset.
    """

    def __init__(self, path):
        self.path = Path(path).absolute()
        self._lock = threading.RLock()
        (self._auto_next, self._automatic_messages,
         self._volume, self._speed, self._output_device), _ = self._read()

    def _check_path(self):
        # Do not resolve(): that would hide destination/ancestor symlinks.
        for candidate in (self.path, *self.path.parents):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise PreferencesError(f"Niebezpieczna ścieżka ustawień: {candidate}")
            expected = stat.S_ISREG if candidate == self.path else stat.S_ISDIR
            if not expected(info.st_mode):
                raise PreferencesError(f"Nieprawidłowa ścieżka ustawień: {candidate}")

    def _read(self):
        self._check_path()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return (True, True, 100, 1.0, ""), False
        except (ValueError, UnicodeError) as error:
            raise PreferencesError(f"Uszkodzony plik ustawień: {self.path}") from error
        if (type(data) is not dict
                or not {"version", "auto_next"} <= set(data)
                or not set(data) <= {"version", "auto_next", "automatic_messages", "volume", "speed", "output_device"}
                or type(data["version"]) is not int or data["version"] != 1
                or type(data["auto_next"]) is not bool
                or type(data.get("automatic_messages", True)) is not bool
                or type(data.get("volume", 100)) is not int
                or not 0 <= data.get("volume", 100) <= 100
                or type(data.get("speed", 1.0)) not in (int, float)
                or data.get("speed", 1.0) not in (1.0, 1.5, 2.0)
                or type(data.get("output_device", "")) is not str
                or "\x00" in data.get("output_device", "")
                or len(data.get("output_device", "")) > 1024):
            raise PreferencesError(f"Nieprawidłowy format ustawień: {self.path}")
        values = (data["auto_next"], data.get("automatic_messages", True),
                  data.get("volume", 100), float(data.get("speed", 1.0)),
                  data.get("output_device", ""))
        complete = {"automatic_messages", "volume", "speed", "output_device"} <= set(data)
        return values, complete

    @property
    def output_device(self):
        return self._output_device

    @property
    def volume(self):
        return self._volume

    @property
    def speed(self):
        return self._speed

    @property
    def automatic_messages(self):
        return self._automatic_messages

    @property
    def auto_next(self):
        return self._auto_next

    def set_output_device(self, value):
        if type(value) is not str:
            raise TypeError("output_device must be a str")
        if "\x00" in value or len(value) > 1024:
            raise ValueError("output_device must have no NUL and at most 1024 characters")
        self._set("output_device", value)

    def set_auto_next(self, value):
        self._set("auto_next", value)

    def set_automatic_messages(self, value):
        self._set("automatic_messages", value)

    def set_volume(self, value):
        if type(value) is not int:
            raise TypeError("volume must be an int")
        if not 0 <= value <= 100:
            raise ValueError("volume must be between 0 and 100")
        self._set("volume", value)

    def set_speed(self, value):
        if type(value) not in (int, float):
            raise TypeError("speed must be numeric")
        if value not in (1.0, 1.5, 2.0):
            raise ValueError("speed must be 1.0, 1.5 or 2.0")
        self._set("speed", float(value))

    def _set(self, field, value):
        if field in ("auto_next", "automatic_messages") and type(value) is not bool:
            raise TypeError(f"{field} must be a bool")
        # UI and playback worker setters share the entire read/save/cache lock.
        with self._lock:
            # Preserve another instance's most recent other fields.
            (auto_next, automatic_messages, volume, speed, output_device), complete = self._read()
            data = {"version": 1, "auto_next": auto_next,
                    "automatic_messages": automatic_messages,
                    "volume": volume, "speed": speed, "output_device": output_device}
            if not complete or data[field] != value:
                data[field] = value
                self._save(data)
            # Refresh stale instances after a successful, write-free operation.
            self._auto_next = data["auto_next"]
            self._automatic_messages = data["automatic_messages"]
            self._volume = data["volume"]
            self._speed = data["speed"]
            self._output_device = data["output_device"]

    def _save(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
