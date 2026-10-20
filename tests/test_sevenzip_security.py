"""Hostile metadata and child lifecycle tests, portable and dependency-free."""
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from test_sevenzip_native import load, PROJECT

HEADER = '7-Zip\n\n--\nPath = fixture.7z\nType = 7z\nPhysical Size = 42\n\n----------\n'
ENTRY = 'Path = chapter.wav\nSize = 3\nAttributes = A\nEncrypted = -\n'

class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.s = load('sevenzip')
        self.m = load('library')
        root = PROJECT / '.test-work/rar7z'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.7z'
        self.path.write_bytes(b'fixture')

    def listing(self, text):
        with mock.patch.object(self.s, '_Reader', return_value=io.BytesIO(text.encode('utf-8'))):
            return self.s.Archive(self.path, sys.executable, error=self.m.LibraryError,
                check_cancel=lambda: None, safe_relative=self.m._safe_relative,
                max_entries=10, max_file=100, max_total=1000)

    def test_reject_ambiguous_or_unsafe_metadata(self):
        bad = [ENTRY + 'Size = 1\n', ENTRY + 'Symbolic Link = target\n',
               ENTRY + 'Hard Link = target\n', ENTRY + 'Reparse = +\n',
               ENTRY + 'Characteristics = SymbolicLink\n', ENTRY + 'Mode = lrwxrwxrwx\n',
               ENTRY.replace('Attributes = A', 'Attributes = A lrwxrwxrwx'),
               ENTRY.replace('Attributes = A', 'Attributes = A prw-rw-rw-'),
               ENTRY.replace('Attributes = A', 'Attributes = 1024'),
               ENTRY.replace('Encrypted = -', 'Encrypted = +'),
               ENTRY.replace('Size = 3', 'Size = rubbish'),
               ENTRY.replace('Size = 3', 'Size = -1'),
               ENTRY + '\n' + ENTRY,
               ENTRY + 'Split Before = +\n', ENTRY + 'Split After = +\n',
               ENTRY.replace('chapter.wav', 'x\nSize = 1\nPath = chapter.wav')]
        for name in ['../x.wav', 'C:\\x.wav', 'x:stream.wav', 'CON.wav', '/abs.wav',
                     'bad\x7fname.wav', 'bad\x85name.wav']:
            bad.append(ENTRY.replace('chapter.wav', name))
        for body in bad:
            with self.subTest(body=body), self.assertRaises(self.m.LibraryError):
                self.listing(HEADER + body)
        for header in [HEADER.replace('Type = 7z', 'Type = zip'),
                       HEADER.replace('Physical Size = 42', 'Volumes = 2'),
                       HEADER.replace('Physical Size = 42', 'Multivolume = +'),
                       'broken']:
            with self.subTest(header=header), self.assertRaises(self.m.LibraryError):
                self.listing(header + ENTRY)

    def test_archive_comment_is_not_interpreted_as_entry_metadata(self):
        header = HEADER.replace('\n\n----------\n', '\nComment = \n{\nA multiline book comment\n}\n\n----------\n')
        archive = self.listing(header + ENTRY)
        self.assertEqual([entry.filename for entry in archive.entries], ['chapter.wav'])
        with self.assertRaises(self.m.LibraryError):
            self.listing(header.replace('A multiline book comment', 'x\n----------\nPath = fake.wav') + ENTRY)

    def test_rar5_empty_link_fields_and_negative_stream_flag_are_regular_files(self):
        text = HEADER.replace('Type = 7z', 'Type = Rar5') + ENTRY
        text += 'Alternate Stream = -\nSymbolic Link = \nHard Link = \nCopy Link = \n'
        archive = self.listing(text)
        self.assertEqual([entry.filename for entry in archive.entries], ['chapter.wav'])
        for line in ('Alternate Stream = +', 'Symbolic Link = -', 'Hard Link = target', 'Copy Link = target'):
            with self.subTest(line=line), self.assertRaises(self.m.LibraryError):
                self.listing(HEADER + ENTRY + line + '\n')

    def test_archive_file_size_is_bounded_before_starting_child(self):
        with mock.patch.object(self.s, '_Reader', side_effect=AssertionError('child started before size validation')) as reader:
            with self.assertRaises(self.m.LibraryError):
                self.s.Archive(self.path, sys.executable, error=self.m.LibraryError,
                    check_cancel=lambda: None, safe_relative=self.m._safe_relative,
                    max_entries=10, max_file=100, max_total=1)
            reader.assert_not_called()

    def test_listing_limit_and_missing_tool_are_presentable(self):
        with mock.patch.object(self.s, 'LIST_LIMIT', 10):
            with self.assertRaises(self.m.LibraryError):
                self.listing(HEADER + ENTRY)
        library = self.m.Library(self.path.parent / 'addons')
        with self.assertRaisesRegex(self.m.LibraryError, '7-Zip'):
            library.import_book(self.path)
        self.assertFalse(library.json_path.exists())

    def test_scan_archive_types_in_natural_order(self):
        for name in ['10.7Z', '2.RAR', '1.zip']:
            (self.path.parent / name).touch()
        self.path.unlink()
        self.assertEqual([p.name for p in self.m.scan_candidates(self.path.parent)], ['1.zip', '2.RAR', '10.7Z'])

class ProcessTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.s = load('sevenzip')
        self.owner = SimpleNamespace(executable=sys.executable, deadline=time.monotonic() + 10,
            check_cancel=lambda: None, unchanged=lambda: None, error=RuntimeError)

    def assert_reaped(self, reader):
        self.assertIsNotNone(reader.process.poll())
        self.assertFalse(any(t.is_alive() for t in reader.threads))
        self.assertTrue(reader.process.stdout.closed)
        self.assertTrue(reader.process.stderr.closed)

    def test_stalled_child_cancelled_and_reaped(self):
        event = threading.Event()
        def cancel():
            if event.is_set():
                raise InterruptedError('cancel')
        self.owner.check_cancel = cancel
        reader = self.s._Reader(self.owner, ['-c', 'import time; time.sleep(30)'])
        timer = threading.Timer(.1, event.set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                reader.read()
        finally:
            timer.join()
            reader.close()
        self.assert_reaped(reader)

    def test_timeout_reaps_stalled_child(self):
        with mock.patch.object(self.s, 'PROCESS_TIMEOUT', .15):
            reader = self.s._Reader(self.owner, ['-c', 'import time; time.sleep(30)'])
        with self.assertRaisesRegex(RuntimeError, 'czas'):
            reader.read()
        self.assert_reaped(reader)

    def test_nonzero_exit_after_stdout_fails_and_stderr_is_bounded(self):
        reader = self.s._Reader(self.owner, ['-c',
            'import sys; sys.stderr.buffer.write(b"e"*2000000); sys.stdout.buffer.write(b"audio"); sys.exit(2)'])
        with self.assertRaises(RuntimeError):
            with reader:
                while reader.read():
                    pass
        self.assertLessEqual(len(reader.errors), 16384)
        self.assert_reaped(reader)

    def test_callback_exception_reaps_backpressured_child(self):
        reader = self.s._Reader(self.owner, ['-c',
            'import sys; sys.stdout.buffer.write(b"a"*2000000)'])
        with self.assertRaises(ValueError):
            with reader:
                reader.read()
                raise ValueError('progress callback')
        self.assert_reaped(reader)

if __name__ == '__main__':
    unittest.main()
