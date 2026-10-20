"""Error-only local diagnostics. No global logger or retained file handles."""
from datetime import datetime
from pathlib import Path
import threading


class ErrorLog:
    def __init__(self, folder, max_bytes=1024 * 1024):
        # Deliberately no I/O until an actual failure is reported.
        self.path = Path(folder) / 'czytelnia.log'
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, stage, detail):
        """Best effort; a diagnostic failure must not replace the actual error."""
        try:
            record = '{} [{}]\n{}\n\n'.format(
                datetime.now().astimezone().isoformat(timespec='seconds'), stage, detail)
            data = record.encode('utf-8', 'replace')
            limit = min(65536, self.max_bytes)
            if len(data) > limit:
                suffix = b'\n[truncated]\n\n'
                data = data[:limit - len(suffix)].decode('utf-8', 'ignore').encode('utf-8') + suffix
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                    self.path.replace(self.path.with_name('czytelnia.log.1'))
                with self.path.open('ab') as stream:
                    stream.write(data)
            return True
        except Exception:
            return False
