"""Duration metadata uses only synthetic files under .test-work."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.test-work' / 'durations'
WORK.mkdir(parents=True, exist_ok=True)


def module(name):
    spec = importlib.util.spec_from_file_location('duration_test_' + name,
        ROOT / 'addon/globalPlugins/czytelnia' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


library = module('library')
player = module('player')
controller = module('controller')


class PersistentDurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for index in range(3):
            (self.source / ('%d.wav' % index)).write_bytes(b'synthetic ' + bytes([index]))
        self.calls = []

    def probe(self, path):
        self.calls.append(Path(path))
        return 60.0 * (int(Path(path).stem) + 1)

    def make_library(self, probe=None):
        return library.Library(self.root / 'config', duration_probe=probe or self.probe)

    def test_import_measures_copied_tracks_and_persists_after_rename_and_restart(self):
        self.assertIn('duration_probe', __import__('inspect').signature(library.Library).parameters)
        lib = self.make_library()
        book = lib.import_book(self.source)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(path.parent.name.startswith('.import-') for path in self.calls))
        self.assertEqual([t['duration'] for t in book['tracks']], [60, 120, 180])
        for index, track in enumerate(book['tracks']):
            info = lib.track_path(book['id'], index).stat()
            self.assertEqual(track['signature'], {'size': info.st_size, 'mtime_ns': info.st_mtime_ns})
        saved = json.loads(lib.json_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['books'], [book])
        again = self.make_library(Mock(side_effect=AssertionError('unexpected probe')))
        self.assertEqual(again.get_book(book['id']), book)
        self.assertEqual(again.import_book(self.source), book)

    def test_playback_metadata_reuses_restart_and_only_changed_file_reprobes(self):
        lib = self.make_library()
        book = lib.import_book(self.source)
        self.calls.clear()
        again = self.make_library()
        self.assertTrue(hasattr(again, 'track_duration'), 'persistent playback duration API missing')
        before = again.json_path.read_bytes()
        for index in range(3):
            self.assertEqual(again.track_duration(book['id'], index), 60 * (index + 1))
        self.assertEqual(self.calls, [])
        self.assertEqual(again.json_path.read_bytes(), before)
        path = again.track_path(book['id'], 1)
        path.write_bytes(b'changed synthetic audio')
        changed_probe = Mock(return_value=222.5)
        again._duration_probe = changed_probe
        self.assertEqual(again.track_duration(book['id'], 1), 222.5)
        changed_probe.assert_called_once_with(path)
        self.assertEqual(again.track_duration(book['id'], 0), 60)
        self.assertEqual(again.track_duration(book['id'], 2), 180)
        final = self.make_library(Mock(side_effect=AssertionError('no repeat probes')))
        self.assertEqual(final.track_duration(book['id'], 1), 222.5)
        final.save_position(book['id'], 1, 10)
        self.assertEqual(self.make_library().get_book(book['id'])['tracks'][1]['duration'], 222.5)

    def test_mtime_only_and_size_only_changes_each_trigger_one_reprobe(self):
        lib = self.make_library()
        book = lib.import_book(self.source)
        path = lib.track_path(book['id'], 0)
        before = path.stat()
        self.calls.clear()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        self.assertEqual(lib.track_duration(book['id'], 0), 60)
        self.assertEqual(self.calls, [path])
        unchanged = path.stat()
        path.write_bytes(b'longer synthetic audio data')
        os.utime(path, ns=(unchanged.st_atime_ns, unchanged.st_mtime_ns))
        self.assertEqual(lib.track_duration(book['id'], 0), 60)
        self.assertEqual(self.calls, [path, path])
        self.assertEqual(lib.track_duration(book['id'], 0), 60)
        self.assertEqual(len(self.calls), 2)

    def test_missing_old_metadata_never_gets_backfilled(self):
        lib = self.make_library()
        book = lib.import_book(self.source)
        document = json.loads(lib.json_path.read_text())
        for track in document['books'][0]['tracks']:
            track.pop('duration')
            track.pop('signature')
        old = json.dumps(document).encode()
        lib.json_path.write_bytes(old)
        probe = Mock(side_effect=AssertionError('migration is forbidden'))
        again = self.make_library(probe)
        self.assertTrue(hasattr(again, 'track_duration'))
        with self.assertRaisesRegex(library.LibraryError, 'ponownie'):
            again.track_duration(book['id'], 0)
        self.assertEqual(again.json_path.read_bytes(), old)
        probe.assert_not_called()

    def test_changed_duration_write_failure_leaves_ram_and_disk_unchanged(self):
        lib = self.make_library()
        book = lib.import_book(self.source)
        self.assertTrue(hasattr(lib, 'track_duration'))
        before = lib.json_path.read_bytes()
        lib.track_path(book['id'], 0).write_bytes(b'changed file')
        lib._duration_probe = Mock(return_value=99)
        with patch.object(library.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(library.LibraryError):
                lib.track_duration(book['id'], 0)
        self.assertEqual(lib.json_path.read_bytes(), before)
        self.assertEqual(lib.get_book(book['id']), book)
        self.assertEqual(lib.track_duration(book['id'], 0), 99)

    def test_invalid_duration_and_probe_failure_roll_back_import(self):
        for bad in (0, -1, True, '60', None, float('inf'), float('nan'), 10 ** 1000):
            with self.subTest(bad=str(bad)[:30]):
                lib = self.make_library(lambda path: bad)
                with self.assertRaises(library.LibraryError):
                    lib.import_book(self.source)
                self.assertEqual(lib.books(), [])
                self.assertFalse(lib.json_path.exists())
                self.assertEqual(list(lib.media_dir.iterdir()), [])
        lib = self.make_library(Mock(side_effect=ValueError('invalid probe JSON')))
        with self.assertRaises(library.LibraryError):
            lib.import_book(self.source)
        self.assertEqual(list(lib.media_dir.iterdir()), [])
        lib = library.Library(self.root / 'config')
        with self.assertRaisesRegex(library.LibraryError, 'ffprobe'):
            lib.import_book(self.source)

    def test_cancel_during_first_probe_stops_import_without_second_probe(self):
        stop = threading.Event()
        def cancel_probe(path):
            self.calls.append(path)
            stop.set()
            return 60
        lib = self.make_library(cancel_probe)
        with self.assertRaises(library.ImportCancelled):
            lib.import_book(self.source, cancel=stop)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(lib.json_path.exists())
        self.assertEqual(list(lib.media_dir.iterdir()), [])

    def test_mutation_during_probe_rolls_back_import(self):
        def mutating_probe(path):
            path.write_bytes(b'modified during probe')
            return 60
        lib = self.make_library(mutating_probe)
        with self.assertRaises(library.LibraryError):
            lib.import_book(self.source)
        self.assertFalse(lib.json_path.exists())
        self.assertEqual(list(lib.media_dir.iterdir()), [])

    def test_zip_import_also_probes_each_copied_track(self):
        archive = self.root / 'source.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            for path in sorted(self.source.iterdir()):
                stream.write(path, path.name)
        lib = self.make_library()
        book = lib.import_book(archive)
        self.assertEqual([t['duration'] for t in book['tracks']], [60, 120, 180])
        self.assertEqual(len(self.calls), 3)

    def test_bad_persisted_metadata_is_rejected_without_overwriting(self):
        lib = self.make_library()
        lib.import_book(self.source)
        saved = lib.json_path.read_bytes()
        for field, bad in (('duration', 0), ('duration', True), ('duration', '60'),
                           ('duration', float('inf')), ('signature', {}),
                           ('signature', {'size': -1, 'mtime_ns': 2}),
                           ('signature', {'size': 1, 'mtime_ns': True})):
            with self.subTest(field=field, bad=bad):
                document = json.loads(saved)
                document['books'][0]['tracks'][0][field] = bad
                payload = json.dumps(document).encode()
                lib.json_path.write_bytes(payload)
                with self.assertRaises(library.LibraryError):
                    self.make_library()
                self.assertEqual(lib.json_path.read_bytes(), payload)


@unittest.skipUnless(os.name == 'nt', 'bundled ffprobe/ffplay require Windows Python')
class RealDurationIntegrationTests(unittest.TestCase):
    def test_real_import_restart_playback_status_and_one_changed_file(self):
        import time
        import subprocess
        with tempfile.TemporaryDirectory(dir=WORK, prefix='real-') as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            def silence(path, seconds):
                with wave.open(str(path), 'wb') as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(8000)
                    output.writeframes(b'\x00\x00' * (8000 * seconds))
            for index, duration in enumerate((6, 8, 10)):
                silence(source / ('%d.wav' % index), duration)
            probe_exe, play_exe = ROOT / 'ffprobe.exe', ROOT / 'ffplay.exe'
            self.assertTrue(probe_exe.is_file())
            self.assertTrue(play_exe.is_file())
            calls, launches = [], []
            real_run, real_popen = subprocess.run, subprocess.Popen
            def guarded_run(command, **kwargs):
                self.assertEqual(Path(command[0]), probe_exe)
                self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file,pipe')
                self.assertEqual(kwargs['timeout'], 10)
                self.assertFalse(kwargs['shell'])
                calls.append(command[-1])
                return real_run(command, **kwargs)
            def guarded_popen(command, **kwargs):
                self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file,pipe')
                if Path(command[0]) == play_exe:
                    self.assertEqual(os.environ.get('SDL_AUDIODRIVER'), 'dummy')
                    self.assertIn('=0,azmq=', command[command.index('-af') + 1])
                    launches.append(command[-1])
                else:
                    self.assertEqual(Path(command[0]), probe_exe)
                return real_popen(command, **kwargs)
            def guarded_native(command, **kwargs):
                self.assertEqual(kwargs['env']['SDL_AUDIODRIVER'], 'dummy')
                self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file,pipe')
                self.assertIn('=0,azmq=', command[command.index('-af') + 1])
                launches.append(command[-1])
                return player.NativeProcess(command, **kwargs)
            probe = lambda path: player.probe_duration(path, probe_exe)
            with patch.object(subprocess, 'run', side_effect=guarded_run), \
                 patch.object(subprocess, 'Popen', side_effect=guarded_popen), \
                 patch.dict(os.environ, {'SDL_AUDIODRIVER': 'dummy'}):
                lib = library.Library(root / 'config', duration_probe=probe)
                book = lib.import_book(source)
                self.assertEqual([t['duration'] for t in book['tracks']], [6, 8, 10])
                self.assertEqual(len(calls), 3)
                lib.save_position(book['id'], 1, 2)
                # New library and new player: no process/RAM cache survives.
                again = library.Library(root / 'config', duration_probe=probe)
                events, pending = [], []
                c = controller.Controller(again,
                    lambda callback: player.Player(play_exe, probe_exe, callback, volume=0,
                                                   native_factory=guarded_native),
                    pending.append, events.append)
                try:
                    c.start(book['id'])
                    deadline = time.monotonic() + 4
                    while c.player.position <= 2.1 and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertGreater(c.player.position, 2.1, events)
                    self.assertEqual(c.player.duration, 8)
                    self.assertRegex(c.book_status(), r'^1[0-6] sekund do końca książki\.$')
                    self.assertEqual(sum(t['duration'] for t in again.get_book(book['id'])['tracks']), 24)
                    before = again.json_path.read_bytes()
                    for _ in range(5):
                        self.assertRegex(c.book_status(), r'^1[0-6] sekund do końca książki\.$')
                    self.assertEqual(again.json_path.read_bytes(), before)
                    c.toggle()
                    self.assertFalse(c.player.is_playing)
                    c.toggle()
                    self.assertTrue(c.player.is_playing)
                    c.next_track()
                    self.assertEqual(c.player.duration, 10)
                    c.toggle()
                    self.assertEqual(len(calls), 3, 'unchanged playback/status must launch zero ffprobes')
                    silence(again.track_path(book['id'], 0), 7)
                    self.assertEqual(c.book_status(), controller.format_time(10 - c.player.position) + ' do końca książki.')
                    self.assertEqual(sum(t['duration'] for t in again.get_book(book['id'])['tracks']), 25)
                    self.assertEqual(len(calls), 4, 'only changed media is re-probed')
                finally:
                    c.close()
                restored = library.Library(root / 'config', duration_probe=probe)
                self.assertEqual(restored.track_duration(book['id'], 0), 7)
                self.assertEqual(len(calls), 4)
                self.assertFalse(c.player._workers)
                report = dict(import_ffprobes=3, unchanged_playback_status_ffprobes=0,
                              changed_file_ffprobes=1, playback_launches=len(launches),
                              stored_durations=[t['duration'] for t in restored.get_book(book['id'])['tracks']],
                              status='passed', residual_workers=len(c.player._workers))
                (WORK / 'real-ffprobe-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                print(json.dumps(report))


if __name__ == '__main__':
    unittest.main()
