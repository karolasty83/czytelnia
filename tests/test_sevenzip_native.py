"""Native bundled 7-Zip integration; no NVDA import or installed packages."""
import importlib.util
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import wave
import zlib

PROJECT = Path(__file__).resolve().parents[1]
SEVEN = PROJECT / 'tools/7z.exe'

def load(name):
    spec = importlib.util.spec_from_file_location('isolated_' + name, PROJECT / 'addon/globalPlugins/czytelnia' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def wav():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as out:
        out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        out.writeframes(b'\0\0' * 800)
    return stream.getvalue()

@unittest.skipUnless(os.name == 'nt', 'Native Windows 7-Zip required')
class NativeTests(unittest.TestCase):
    def setUp(self):
        root = PROJECT / '.test-work/rar7z'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.m = load('library')
        self.probes = []

    def probe(self, path):
        with wave.open(str(path)) as stream:
            duration = stream.getnframes() / stream.getframerate()
        self.probes.append(path)
        return duration

    def command(self, *args, cwd=None):
        result = subprocess.run([str(SEVEN), *map(str, args)], cwd=cwd, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def archive(self):
        source = self.root / 'source'
        (source / 'żółć').mkdir(parents=True)
        for name in ['10.wav', '2.wav']:
            (source / 'żółć' / name).write_bytes(wav())
        path = self.root / 'Książka.7z'
        self.command('a', '-t7z', path, '.', cwd=source)
        return path

    def test_native_7z_import_persistence_order_progress_and_duplicate(self):
        archive = self.archive()
        self.command('t', archive)
        self.assertIn('sevenzip_path', __import__('inspect').signature(self.m.Library).parameters)
        library = self.m.Library(self.root / 'addons', duration_probe=self.probe, sevenzip_path=SEVEN)
        stages = []
        book = library.import_book(archive, stage_progress=lambda s,d,t: stages.append((s,d/t,len(self.probes))))
        self.assertEqual(book['title'], 'Książka')
        self.assertEqual([t['title'] for t in book['tracks']], ['2', '10'])
        self.assertEqual([t['duration'] for t in book['tracks']], [.1, .1])
        self.assertEqual([library.track_path(book['id'], i).read_bytes() for i in range(2)], [wav(), wav()])
        self.assertEqual([s[1] for s in stages], sorted(s[1] for s in stages))
        self.assertEqual(stages[-1], ('complete', 1, 2))
        self.assertTrue(all(s[1] < 1 for s in stages[:-1]))
        self.assertEqual(library.import_book(archive), book)
        self.assertEqual(len(self.probes), 2)
        self.assertEqual(self.m.Library(self.root / 'addons').books(), [book])

    def rar(self, names):
        def header(kind, flags, payload):
            data = struct.pack('<BHH', kind, flags, 7 + len(payload)) + payload
            return struct.pack('<H', zlib.crc32(data) & 65535) + data
        data = bytearray(b'Rar!\x1a\x07\x00')
        data += header(0x73, 0, b'\0' * 6)
        for name in names:
            content = wav()
            fields = struct.pack('<IIBIIBBHI', len(content), len(content), 2,
                                 zlib.crc32(content), 0, 20, 0x30, len(name), 0x20)
            data += header(0x74, 0x8000, fields + name) + content
        data += header(0x7b, 0, b'')
        path = self.root / 'store.rar'
        path.write_bytes(data)
        self.command('t', path)
        return path

    def test_native_rar_store_import(self):
        path = self.rar([b'CD/10.wav', b'CD/2.wav'])
        library = self.m.Library(self.root / 'addons', duration_probe=self.probe, sevenzip_path=SEVEN)
        book = library.import_book(path)
        self.assertEqual([t['title'] for t in book['tracks']], ['2', '10'])
        self.assertEqual(library.track_path(book['id'], 0).read_bytes(), wav())

    def test_native_wildcards_are_exact_not_patterns(self):
        path = self.rar([b'*.wav', b'?.wav', b'other.wav'])
        library = self.m.Library(self.root / 'addons', duration_probe=self.probe, sevenzip_path=SEVEN)
        book = library.import_book(path)
        self.assertEqual(len(book['tracks']), 3)
        for i in range(3):
            self.assertEqual(library.track_path(book['id'], i).read_bytes(), wav())

    def test_crc_failure_preserves_old_json(self):
        library = self.m.Library(self.root / 'addons', duration_probe=self.probe, sevenzip_path=SEVEN)
        library.import_book(self.archive())
        before = library.json_path.read_bytes()
        path = self.rar([b'bad.wav'])
        data = bytearray(path.read_bytes())
        data[data.index(b'RIFF') + 50] ^= 1
        path.write_bytes(data)
        with self.assertRaises(self.m.LibraryError):
            library.import_book(path)
        self.assertEqual(library.json_path.read_bytes(), before)
        self.assertEqual(len(list(library.media_dir.iterdir())), 1)

    def test_native_cancellation_reaps_process_and_cleans_stage(self):
        from unittest import mock
        import threading
        event = threading.Event()
        path = self.archive()
        library = self.m.Library(self.root / 'addons', duration_probe=self.probe, sevenzip_path=SEVEN)
        children = []
        popen = subprocess.Popen
        def start(*a, **kw):
            process = popen(*a, **kw)
            children.append(process)
            return process
        with mock.patch.object(subprocess, 'Popen', side_effect=start):
            with self.assertRaises(self.m.ImportCancelled):
                library.import_book(path, cancel=event,
                    progress=lambda done, total: event.set() if done else None)
        self.assertGreaterEqual(len(children), 2)
        self.assertTrue(all(p.poll() is not None for p in children))
        self.assertFalse(any(t.name.startswith('czytelnia-7z') for t in threading.enumerate()))
        self.assertFalse(library.json_path.exists())
        self.assertEqual(list(library.media_dir.iterdir()), [])

    def test_source_change_during_last_probe_rolls_back(self):
        archive = self.archive()
        def probe(path):
            result = self.probe(path)
            if len(self.probes) == 2:
                archive.write_bytes(archive.read_bytes() + b'changed')
            return result
        library = self.m.Library(self.root / 'addons', duration_probe=probe, sevenzip_path=SEVEN)
        with self.assertRaisesRegex(self.m.LibraryError, 'zmieni'):
            library.import_book(archive)
        self.assertEqual(library.books(), [])
        self.assertFalse(library.json_path.exists())
        self.assertEqual(list(library.media_dir.iterdir()), [])

    def test_option_and_listfile_names_are_literal(self):
        source = self.root / 'source'
        source.mkdir()
        for name in ['@tracks.wav', '-x.wav']:
            (source / name).write_bytes(wav())
        archive = self.root / 'literal.7z'
        self.command('a', archive, '.', cwd=source)
        library = self.m.Library(self.root / 'addons', duration_probe=self.probe, sevenzip_path=SEVEN)
        book = library.import_book(archive)
        self.assertEqual(len(book['tracks']), 2)
        for i in range(2):
            self.assertEqual(library.track_path(book['id'], i).read_bytes(), wav())

    def test_encrypted_and_limits_rejected_without_committing(self):
        from unittest import mock
        archive = self.archive()
        library = self.m.Library(self.root / 'addons', duration_probe=self.probe, sevenzip_path=SEVEN)
        for field, limit in [('MAX_ENTRIES', 1), ('MAX_FILE_BYTES', 12), ('MAX_TOTAL_BYTES', 12)]:
            with self.subTest(field=field), mock.patch.object(self.m, field, limit):
                with self.assertRaises(self.m.LibraryError):
                    library.import_book(archive)
                self.assertEqual(library.books(), [])
                self.assertFalse(library.json_path.exists())
        encrypted = self.root / 'encrypted.7z'
        self.command('a', '-pfixture-password', encrypted, '.', cwd=self.root / 'source')
        with self.assertRaisesRegex(self.m.LibraryError, 'zaszyfrowan'):
            library.import_book(encrypted)

if __name__ == '__main__':
    unittest.main()
