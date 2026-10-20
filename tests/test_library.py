"""Isolated stdlib tests; importing this does not load NVDA's plugin."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
MODULE = PROJECT / "addon/globalPlugins/czytelnia/library.py"


def load_library():
    spec = importlib.util.spec_from_file_location("czytelnia_library_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "Brak modułu biblioteki")
        self.module = load_library()
        root = PROJECT / ".test-work"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, prefix="library-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addons = self.root / "addons"
        self.library = self.module.Library(self.addons, duration_probe=lambda path: 60.0)

    def make_source(self):
        source = self.root / "Książka"
        source.mkdir()
        (source / "10.mp3").write_bytes(b"ten")
        (source / "2.mp3").write_bytes(b"two")
        (source / "1.M4B").write_bytes(b"one")
        (source / "cover.txt").write_text("ok", encoding="utf-8")
        return source

    def require_symlink(self, *, directory=False):
        """Probe this filesystem/token, skipping only unsupported link creation."""
        with tempfile.TemporaryDirectory(dir=self.root, prefix="symlink-probe-") as temp:
            target = Path(temp) / "target"
            if directory:
                target.mkdir()
            else:
                target.write_bytes(b"probe")
            link = Path(temp) / "link"
            try:
                link.symlink_to(target, target_is_directory=directory)
            except (OSError, NotImplementedError) as error:
                if not (isinstance(error, NotImplementedError)
                        or getattr(error, "winerror", None) == 1314
                        or getattr(error, "errno", None) in
                        (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EOPNOTSUPP)):
                    raise
                kind = "directory" if directory else "file"
                code = (f"WinError {error.winerror}" if getattr(error, "winerror", None)
                        else f"errno {getattr(error, 'errno', None)}")
                self.skipTest(f"{kind} symlink probe unavailable: {code} ({type(error).__name__})")
            else:
                link.unlink()

    def make_zip(self, entries):
        path = self.root / "Książka.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries:
                if isinstance(name, str):
                    info = zipfile.ZipInfo(name)
                    # ZipInfo normalizes backslashes on Windows. Restore the literal
                    # filename so both ZIP headers contain the intended hostile bytes.
                    info.filename = name
                else:
                    info = name
                archive.writestr(info, content)
        return path

    def test_directory_import_copies_sorted_media_and_persists(self):
        source = self.make_source()
        self.assertTrue(hasattr(self.library, "import_book"), "Brak importowania")
        book = self.library.import_book(source)
        self.assertRegex(book["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(book["title"], "Książka")
        self.assertEqual([t["title"] for t in book["tracks"]], ["1", "2", "10"])
        self.assertEqual((book["track_index"], book["position"]), (0, 0.0))
        owned = self.addons / "czytelnia-biblioteka" / book["id"]
        self.assertEqual([(owned / t["path"]).read_bytes() for t in book["tracks"]],
                         [b"one", b"two", b"ten"])
        self.assertTrue((source / "1.M4B").exists())
        persisted = self.module.Library(self.addons)
        self.assertEqual(persisted.books(), [book])
        book["tracks"][0]["title"] = "changed"
        detached = persisted.books()
        detached[0]["tracks"].clear()
        self.assertEqual(len(persisted.books()[0]["tracks"]), 3)
        self.assertEqual(self.library.books()[0]["tracks"][0]["title"], "1")

    def test_zip_import_is_generated_sorted_and_rejects_unsafe_entries(self):
        archive = self.make_zip([("CD/10.mp3", b"ten"), ("CD/2.mp3", b"two")])
        with self.subTest(case="valid archive"):
            book = self.library.import_book(archive)
            self.assertEqual([t["title"] for t in book["tracks"]], ["2", "10"])
            owned = self.addons / "czytelnia-biblioteka" / book["id"]
            self.assertEqual([(owned / t["path"]).read_bytes() for t in book["tracks"]],
                             [b"two", b"ten"])
        for unsafe in ("../escape.txt", "/abs.txt", "C:/drive.txt", "safe:stream.txt",
                       "folder/../../escape.txt", "folder\\escape.txt", "CON.txt"):
            with self.subTest(unsafe=unsafe):
                archive = self.make_zip([("1.mp3", b"ok"), (unsafe, b"bad")])
                self.assertEqual(archive.read_bytes().count(unsafe.encode("ascii")), 2)
                with zipfile.ZipFile(archive) as fixture:
                    self.assertEqual(fixture.infolist()[1].orig_filename, unsafe)
                    self.assertIsNone(fixture.testzip(), "Hostile ZIP fixture must have valid CRCs")
                with self.assertRaises(self.module.LibraryError):
                    self.library.import_book(archive)
        info = zipfile.ZipInfo("link.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.make_zip([("1.mp3", b"ok"), (info, b"/outside")])
        with self.assertRaises(self.module.LibraryError):
            self.library.import_book(archive)

    def test_source_symlinks_are_rejected_including_non_audio(self):
        self.require_symlink()
        source = self.make_source()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "hidden.mp3").write_bytes(b"secret")
        link = source / "innocent.txt"
        link.symlink_to(outside / "hidden.mp3")
        with self.assertRaises(self.module.LibraryError):
            self.library.import_book(source)

    def test_source_directory_symlinks_are_rejected(self):
        self.require_symlink(directory=True)
        source = self.make_source()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "hidden.mp3").write_bytes(b"secret")
        link = source / "innocent.txt"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(self.module.LibraryError):
            self.library.import_book(source)
        with self.assertRaises(self.module.LibraryError):
            self.library.import_book(link)

    def test_positions_paths_and_duplicates_survive_reload(self):
        source = self.make_source()
        book = self.library.import_book(source)
        self.assertTrue(hasattr(self.library, "get_book"), "Brak odczytu książki")
        self.assertEqual(self.library.get_book(book["id"]), book)
        self.library.save_position(book["id"], 1, 12.5)
        again = self.module.Library(self.addons)
        self.assertEqual(again.get_book(book["id"])["position"], 12.5)
        self.assertEqual(again.get_book(book["id"])["track_index"], 1)
        self.assertEqual(again.track_path(book["id"], 1).read_bytes(), b"two")
        self.assertEqual(again.import_book(source)["id"], book["id"])
        self.assertEqual(len(again.books()), 1)
        for index, seconds in ((-1, 0), (3, 1), (True, 2), (0, -1),
                               (0, float("nan")), (0, float("inf")), (0, "1")):
            with self.subTest(index=index, seconds=seconds):
                with self.assertRaises(self.module.LibraryError):
                    again.save_position(book["id"], index, seconds)
        for bad_id in ("../outside", "f" * 32, "", None):
            with self.subTest(book_id=bad_id):
                with self.assertRaises(self.module.LibraryError):
                    again.track_path(bad_id, 0)
        self.assertEqual(again.get_book(book["id"])["position"], 12.5)
        self.assertEqual(self.module.Library(self.addons).books(), again.books())

    def test_corrupt_and_tampered_json_are_never_overwritten(self):
        book = self.library.import_book(self.make_source())
        path = self.addons / "czytelnia_config.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        payloads = [b"{broken", b"[]", b'{"version":999,"books":[]}']
        for field, value in (("id", "../outside"), ("tracks", [{"path": "../outside.mp3", "title": "x"}]),
                             ("tracks", []), ("position", -1), ("track_index", 999)):
            data = json.loads(json.dumps(original))
            data["books"][0][field] = value
            payloads.append(json.dumps(data).encode())
        for payload in payloads:
            with self.subTest(payload=payload[:100]):
                path.write_bytes(payload)
                with self.assertRaises(self.module.LibraryError):
                    self.module.Library(self.addons)
                self.assertEqual(path.read_bytes(), payload)
        # An already-open instance must not overwrite corruption, either.
        path.write_bytes(b"broken after opening")
        with self.assertRaises(self.module.LibraryError):
            self.library.save_position(book["id"], 0, 1)
        self.assertEqual(path.read_bytes(), b"broken after opening")

    def test_remove_only_owned_media_and_rollback_json_failure(self):
        source = self.make_source()
        book = self.library.import_book(source)
        self.assertTrue(hasattr(self.library, "remove_book"), "Brak usuwania")
        before = (self.addons / "czytelnia_config.json").read_bytes()
        with mock.patch.object(self.module.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(self.module.LibraryError):
                self.library.remove_book(book["id"])
        self.assertEqual((self.addons / "czytelnia_config.json").read_bytes(), before)
        self.assertEqual(self.library.track_path(book["id"], 0).read_bytes(), b"one")
        self.library.remove_book(book["id"])
        self.assertEqual(self.module.Library(self.addons).books(), [])
        self.assertFalse((self.addons / "czytelnia-biblioteka" / book["id"]).exists())
        self.assertTrue((source / "1.M4B").exists())
        for bad_id in ("../Książka", str(source), "", "f" * 32):
            with self.assertRaises(self.module.LibraryError):
                self.library.remove_book(bad_id)
        self.assertTrue(source.exists())

    def test_remove_refuses_symlink_media_and_tampered_track(self):
        self.require_symlink(directory=True)
        source = self.make_source()
        book = self.library.import_book(source)
        owned = self.addons / "czytelnia-biblioteka" / book["id"]
        shutil.rmtree(owned)
        owned.symlink_to(source, target_is_directory=True)
        with self.assertRaises(self.module.LibraryError):
            self.library.remove_book(book["id"])
        with self.assertRaises(self.module.LibraryError):
            self.library.track_path(book["id"], 0)
        self.assertTrue((source / "1.M4B").exists())

    def test_atomic_import_and_save_failures_leave_previous_state(self):
        book = self.library.import_book(self.make_source())
        before = (self.addons / "czytelnia_config.json").read_bytes()
        archive = self.make_zip([("3.mp3", b"three")])
        with mock.patch.object(self.module.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(archive)
            with self.assertRaises(self.module.LibraryError):
                self.library.save_position(book["id"], 0, 42)
        self.assertEqual(self.library.books(), [book])
        self.assertEqual((self.addons / "czytelnia_config.json").read_bytes(), before)
        self.assertEqual([p.name for p in (self.addons / "czytelnia-biblioteka").iterdir()], [book["id"]])
        self.assertFalse(list(self.addons.glob("*.tmp")))

    def test_cancellation_and_progress_use_streamed_copy_and_rollback(self):
        source = self.make_source()
        self.assertTrue(hasattr(self.module, "ImportCancelled"), "Brak anulowania")
        with self.assertRaises(self.module.ImportCancelled):
            self.library.import_book(source, cancel=lambda: True)
        progress = []
        with mock.patch.object(self.module, "COPY_CHUNK_SIZE", 2):
            with self.assertRaises(self.module.ImportCancelled):
                self.library.import_book(source, progress=lambda done, total: progress.append((done, total)),
                                         cancel=lambda: bool(progress and progress[-1][0] > 0))
        self.assertEqual(self.library.books(), [])
        self.assertFalse((self.addons / "czytelnia_config.json").exists())
        self.assertFalse(list((self.addons / "czytelnia-biblioteka").iterdir()))
        progress.clear()
        self.library.import_book(source, progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress[-1], (9, 9))
        self.assertEqual(sorted(progress), progress)

    def test_entry_size_and_disk_limits_fail_without_import(self):
        source = self.make_source()
        archive = self.make_zip([("1.mp3", b"12345"), ("note.txt", b"12345")])
        self.assertTrue(hasattr(self.module, "MAX_ENTRIES"), "Brak limitów")
        for target, limit, value in ((source, "MAX_ENTRIES", 2), (archive, "MAX_ENTRIES", 1),
                                      (source, "MAX_FILE_BYTES", 2), (archive, "MAX_FILE_BYTES", 4),
                                      (source, "MAX_TOTAL_BYTES", 5), (archive, "MAX_TOTAL_BYTES", 5)):
            with self.subTest(target=target.name, limit=limit):
                with mock.patch.object(self.module, limit, value):
                    with self.assertRaises(self.module.LibraryError):
                        self.library.import_book(target)
        with mock.patch.object(self.module.shutil, "disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(self.module.LibraryError, "miejsca"):
                self.library.import_book(source)
        self.assertEqual(self.library.books(), [])
        self.assertFalse((self.addons / "czytelnia_config.json").exists())

    def test_scan_candidates_finds_archives_and_direct_audio_not_child_books(self):
        source = self.make_source()
        archive = self.make_zip([("1.mp3", b"one")])
        (self.root / "empty").mkdir()
        (self.root / "root.mp3").write_bytes(b"root")
        self.assertTrue(hasattr(self.module, "scan_candidates"), "Brak wyszukiwania książek")
        candidates = self.module.scan_candidates(self.root)
        self.assertEqual(set(candidates), {self.root, archive})
        self.assertTrue(all(isinstance(p, Path) for p in candidates))
        self.assertEqual(self.module.scan_candidates(source), [source])

    def test_scan_candidates_rejects_directory_symlinks(self):
        self.require_symlink(directory=True)
        source = self.make_source()
        (self.root / "link").symlink_to(source, target_is_directory=True)
        with self.assertRaises(self.module.LibraryError):
            self.module.scan_candidates(self.root)

    def test_uuid_collision_never_deletes_existing_book(self):
        first = self.library.import_book(self.make_source())
        archive = self.make_zip([("other.mp3", b"other")])
        with mock.patch.object(self.module.uuid, "uuid4", return_value=mock.Mock(hex=first["id"])):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(archive)
        self.assertEqual(self.library.track_path(first["id"], 0).read_bytes(), b"one")
        self.assertEqual(self.module.Library(self.addons).books(), [first])

    def test_storage_cannot_be_imported_as_source(self):
        first = self.library.import_book(self.make_source())
        owned = self.addons / "czytelnia-biblioteka" / first["id"]
        for source in (owned, self.addons, self.root):
            with self.subTest(source=source):
                with self.assertRaises(self.module.LibraryError):
                    self.library.import_book(source)
        self.assertEqual(self.library.books(), [first])

    def test_zip_metadata_is_bounded_before_loading_entries(self):
        archive = self.make_zip([("1.mp3", b"one")])
        # Malicious central-directory length must be rejected before ZipFile allocates it.
        import struct
        content = bytearray(archive.read_bytes())
        eocd = content.rfind(b"PK\x05\x06")
        struct.pack_into("<I", content, eocd + 12, 0x7fffffff)
        archive.write_bytes(content)
        with mock.patch.object(self.module.zipfile, "ZipFile", side_effect=AssertionError("Unbounded ZIP parser invoked")):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(archive)

    def test_thread_event_cancels_zip_and_concurrent_save_is_serialized(self):
        import threading
        first = self.library.import_book(self.make_source())
        archive = self.make_zip([("next.mp3", b"abcdef")])
        stop = threading.Event()
        stop.set()
        with self.assertRaises(self.module.ImportCancelled):
            self.library.import_book(archive, cancel=stop)
        entered = threading.Event()
        release = threading.Event()
        save_started = threading.Event()
        errors = []
        def progress(done, total):
            if done:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("Test synchronization timeout")
        def importer():
            try:
                self.library.import_book(archive, progress=progress)
            except Exception as error:
                errors.append(error)
        def saver():
            save_started.set()
            try:
                self.library.save_position(first["id"], 1, 42)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=importer)
        writer = threading.Thread(target=saver)
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            writer.start()
            self.assertTrue(save_started.wait(10))
        finally:
            release.set()
            worker.join(10)
            if writer.ident is not None:
                writer.join(10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        reloaded = self.module.Library(self.addons)
        self.assertEqual(len(reloaded.books()), 2)
        self.assertEqual(reloaded.get_book(first["id"])["position"], 42)

    def test_crc_failure_and_late_disk_exhaustion_rollback(self):
        archive = self.make_zip([("1.mp3", b"123456789")])
        raw = archive.read_bytes().replace(b"123456789", b"987654321", 1)
        archive.write_bytes(raw)
        with self.assertRaises(self.module.LibraryError):
            self.library.import_book(archive)
        self.assertEqual(self.library.books(), [])
        source = self.make_source()
        with mock.patch.object(self.module.shutil, "disk_usage", side_effect=[mock.Mock(free=10**12), mock.Mock(free=0)]):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(source)
        self.assertFalse((self.addons / "czytelnia_config.json").exists())
        self.assertEqual(list((self.addons / "czytelnia-biblioteka").iterdir()), [])

    def test_json_symlink_is_refused(self):
        self.require_symlink()
        outside = self.root / "outside.json"
        outside.write_text('{"version":1,"books":[]}', encoding="utf-8")
        self.addons.mkdir()
        json_path = self.addons / "czytelnia_config.json"
        json_path.symlink_to(outside)
        with self.assertRaises(self.module.LibraryError):
            self.module.Library(self.addons)
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"version":1,"books":[]}')

    def test_windows_reparse_point_is_refused(self):
        from types import SimpleNamespace
        source = self.make_source()
        original_lstat = Path.lstat
        def reparse(path, *args, **kwargs):
            if path == source:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)
        with mock.patch.object(Path, "lstat", reparse):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(source)

    def test_track_symlink_is_refused(self):
        self.require_symlink()
        source = self.make_source()
        book = self.library.import_book(source)
        track = self.library.track_path(book["id"], 0)
        track.unlink()
        track.symlink_to(source / "1.M4B")
        with self.assertRaises(self.module.LibraryError):
            self.library.track_path(book["id"], 0)
        with self.assertRaises(self.module.LibraryError):
            self.library.remove_book(book["id"])
        self.assertTrue((source / "1.M4B").exists())

    def test_permission_failure_is_presentable_and_huge_position_is_rejected(self):
        source = self.make_source()
        with mock.patch.object(self.module.shutil, "disk_usage", side_effect=PermissionError("denied")):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(source)
        book = self.library.import_book(source)
        with self.assertRaises(self.module.LibraryError):
            self.library.save_position(book["id"], 0, 10 ** 1000)
        self.assertEqual(self.library.get_book(book["id"])["position"], 0)

    def test_duplicate_refuses_media_replaced_by_link(self):
        self.require_symlink()
        source = self.make_source()
        book = self.library.import_book(source)
        track = self.library.track_path(book["id"], 0)
        track.unlink()
        track.symlink_to(source / "1.M4B")
        with self.assertRaises(self.module.LibraryError):
            self.library.import_book(source)

    def test_json_growth_limit_rolls_back_and_zip64_can_import(self):
        self.assertTrue(hasattr(self.module, "MAX_JSON_BYTES"), "Brak limitu zapisu JSON")
        source = self.make_source()
        with mock.patch.object(self.module, "MAX_JSON_BYTES", 32):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(source)
        self.assertFalse((self.addons / "czytelnia_config.json").exists())
        self.assertEqual(self.library.books(), [])
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 1):
            archive = self.make_zip([("2.mp3", b"zip64")])
        book = self.library.import_book(archive)
        self.assertEqual(self.library.track_path(book["id"], 0).read_bytes(), b"zip64")
        self.assertEqual(self.module.Library(self.addons).books(), [book])

    def test_zip64_metadata_cannot_bypass_preflight(self):
        import struct
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 1):
            archive = self.make_zip([("1.mp3", b"one")])
        content = bytearray(archive.read_bytes())
        record = content.rfind(b"PK\x06\x06")
        self.assertGreaterEqual(record, 0)
        struct.pack_into("<Q", content, record + 40, 0x7fffffff)
        archive.write_bytes(content)
        with mock.patch.object(self.module.zipfile, "ZipFile", side_effect=AssertionError("Unbounded ZIP64 parser invoked")):
            with self.assertRaises(self.module.LibraryError):
                self.library.import_book(archive)

    def test_empty_library_does_not_create_json(self):
        self.assertEqual(self.library.books(), [])
        self.assertFalse((self.addons / "czytelnia_config.json").exists())


if __name__ == "__main__":
    unittest.main()
