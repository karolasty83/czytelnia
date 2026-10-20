"""Book totals reuse imported metadata; only changed files need a probe."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_controller as fixtures
from test_persistent_durations import library, controller, WORK


class BookStatusTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.durations = [60, 120, 180]
        for index in range(3):
            (self.source / ('%d.wav' % index)).write_bytes(b'synthetic audio fixture')
        self.probe = Mock(side_effect=lambda path: self.durations[int(path.stem)])
        self.lib = library.Library(self.root / 'config', duration_probe=self.probe)
        self.book = self.lib.import_book(self.source)
        self.paths = [self.lib.track_path(self.book['id'], i) for i in range(3)]
        self.c = controller.Controller(self.lib, fixtures.Player, lambda cb: cb(), lambda msg: None)
        self.c.player._ffprobe = 'must-not-probe-after-import'
        self.probe.reset_mock()

    def test_remaining_only_follows_position_without_load_probe_or_write(self):
        self.assertEqual(self.c.book_status(), 'Nie wybrano książki.')
        self.c.start(self.book['id'], index=1)
        self.c.player.position = 30
        with patch.object(self.c.player, 'load', side_effect=AssertionError('status must not load')), \
             patch.object(self.c.player, 'pause', side_effect=AssertionError('status must not pause')), \
             patch.object(self.lib, '_write', side_effect=AssertionError('status must not write')):
            self.assertEqual(self.c.book_status(), '4 minuty do końca książki.')
            self.c.player.position = 0
            self.assertEqual(self.c.book_status(), '5 minut do końca książki.')
        self.probe.assert_not_called()
        self.assertTrue(self.c.player.is_playing)
    def test_remaining_uses_latest_speed_without_changing_source_metadata(self):
        self.c.start(self.book['id'], index=1)
        self.c.player.position = 30
        self.c.save()
        before = self.lib.json_path.read_bytes()
        snapshot = self.c.book_status_snapshot()
        results = self.c.probe_book_status(snapshot, lambda: False)
        for speed, expected in ((1.0, '4 minuty'), (1.5, '3 minuty'), (2.0, '2 minuty')):
            with self.subTest(speed=speed):
                self.c.player.speed = speed
                self.assertEqual(self.c.complete_book_status(snapshot, results),
                                 expected + ' do końca książki.')
                self.assertEqual(self.c.book_status(), expected + ' do końca książki.')
                self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.c.player.position = 90
        self.c.player.pause()
        self.assertEqual(self.c.complete_book_status(snapshot, results),
                         '1 minuta do końca książki.')
        self.probe.assert_not_called()
        stored = self.lib.get_book(self.book['id'])
        self.assertEqual(stored['position'], 30)
        self.assertEqual([track['duration'] for track in stored['tracks']], [60, 120, 180])

    def test_changed_file_reprobes_and_persists_status_across_restart(self):
        self.c.start(self.book['id'], index=1)
        self.paths[0].write_bytes(b'changed file size')
        self.durations[0] = 120
        self.assertEqual(self.c.book_status(), '5 minut do końca książki.')
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['duration'], 120)
        self.probe.assert_called_once_with(self.paths[0])
        self.lib = library.Library(self.root / 'config', duration_probe=Mock(side_effect=AssertionError('repeat')))
        self.c = controller.Controller(self.lib, fixtures.Player, lambda cb: cb(), lambda msg: None)
        self.c.start(self.book['id'], index=1)
        self.assertEqual(self.c.book_status(), '5 minut do końca książki.')
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['duration'], 120)

    def test_missing_metadata_is_unknown_without_migration(self):
        document = json.loads(self.lib.json_path.read_text())
        track = document['books'][0]['tracks'][0]
        track.pop('duration')
        track.pop('signature')
        self.lib.json_path.write_text(json.dumps(document))
        self.lib = library.Library(self.root / 'config', duration_probe=self.probe)
        self.c = controller.Controller(self.lib, fixtures.Player, lambda cb: cb(), lambda msg: None)
        self.c.player._ffprobe = 'must-not-backfill'
        self.c.start(self.book['id'], index=1)
        before = self.lib.json_path.read_bytes()
        self.assertEqual(self.c.book_status(), 'Czas do końca książki: nieznany.')
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.probe.assert_not_called()

    def test_changed_probe_runs_outside_library_lock_and_uses_latest_position(self):
        self.c.start(self.book['id'], index=1)
        self.paths[0].write_bytes(b'changed file size')
        self.durations[0] = 120
        entered, release = threading.Event(), threading.Event()
        def slow_probe(path):
            entered.set()
            if not release.wait(5):
                raise AssertionError('probe not released')
            return self.durations[int(path.stem)]
        self.lib._duration_probe = slow_probe
        snapshot = self.c.book_status_snapshot()
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(self.c.probe_book_status, snapshot, lambda: False)
            try:
                self.assertTrue(entered.wait(5))
                executor.submit(self.lib.save_position, self.book['id'], 1, 30).result(timeout=1)
                self.c.player.position = 30
                self.c.player.speed = 1.5
            finally:
                release.set()
            result = pending.result(timeout=5)
        self.assertEqual(self.c.complete_book_status(snapshot, result),
            '3 minuty do końca książki.')
        self.assertEqual(self.lib.get_book(self.book['id'])['position'], 30)
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['duration'], 120)
    def test_changed_failure_is_unknown_and_retries_without_caching_failure(self):
        self.c.start(self.book['id'], index=1)
        self.paths[0].write_bytes(b'changed file size')
        before = self.lib.json_path.read_bytes()
        for bad in (0, -1, None, float('nan'), float('inf')):
            self.lib._duration_probe = Mock(return_value=bad)
            self.assertEqual(self.c.book_status(), 'Czas do końca książki: nieznany.')
            self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.lib._duration_probe = self.probe
        self.assertEqual(self.c.book_status(), '5 minut do końca książki.')
        self.probe.assert_called_once_with(self.paths[0])
        self.paths[2].unlink()
        self.assertEqual(self.c.book_status(), 'Czas do końca książki: nieznany.')

    def test_mutation_during_probe_or_before_commit_is_never_persisted(self):
        self.c.start(self.book['id'], index=1)
        before = self.lib.json_path.read_bytes()
        self.paths[0].write_bytes(b'changed file')
        def mutate(path):
            path.write_bytes(b'modified during probe')
            return 99
        self.lib._duration_probe = mutate
        self.assertEqual(self.c.book_status(), 'Czas do końca książki: nieznany.')
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.lib._duration_probe = self.probe
        snapshot = self.c.book_status_snapshot()
        result = self.c.probe_book_status(snapshot, lambda: False)
        self.paths[0].write_bytes(b'changed again before commit')
        self.assertEqual(self.c.complete_book_status(snapshot, result), 'Czas do końca książki: nieznany.')
        self.assertEqual(self.lib.json_path.read_bytes(), before)

    def test_cancel_during_probe_stops_next_and_does_not_commit(self):
        self.c.start(self.book['id'], index=1)
        before = self.lib.json_path.read_bytes()
        for path in self.paths:
            path.write_bytes(b'changed file')
        cancelled = threading.Event()
        def cancelling(path):
            cancelled.set()
            return 99
        self.lib._duration_probe = Mock(side_effect=cancelling)
        self.c.cancelled = cancelled.is_set
        snapshot = self.c.book_status_snapshot()
        result = self.c.probe_book_status(snapshot, cancelled.is_set)
        self.assertIsNone(self.c.complete_book_status(snapshot, result))
        self.lib._duration_probe.assert_called_once_with(self.paths[0])
        self.assertEqual(self.lib.json_path.read_bytes(), before)

    def test_new_book_or_closed_controller_rejects_old_result(self):
        self.c.start(self.book['id'], index=1)
        self.paths[0].write_bytes(b'changed file')
        snapshot = self.c.book_status_snapshot()
        result = self.c.probe_book_status(snapshot, lambda: False)
        self.c._book_status_token.set()
        with patch.object(self.lib, '_write', side_effect=AssertionError('stale commit')):
            self.assertIsNone(self.c.complete_book_status(snapshot, result))
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['signature'],
                         self.book['tracks'][0]['signature'])

    def test_status_write_failure_keeps_old_metadata_and_retries(self):
        self.c.start(self.book['id'], index=1)
        before = self.lib.json_path.read_bytes()
        self.paths[0].write_bytes(b'changed file')
        self.durations[0] = 120
        with patch.object(library.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(library.LibraryError):
                self.c.book_status()
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['duration'], 60)
        self.assertEqual(self.c.book_status(), '5 minut do końca książki.')
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['duration'], 120)
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['duration'], 120)

    def test_newer_playback_metadata_wins_over_older_scan(self):
        self.c.start(self.book['id'], index=1)
        self.paths[0].write_bytes(b'changed file')
        snapshot = self.c.book_status_snapshot()
        self.durations[0] = 120
        result = self.c.probe_book_status(snapshot, lambda: False)
        self.lib._duration_probe = lambda path: 180
        self.assertEqual(self.lib.track_duration(self.book['id'], 0), 180)
        self.assertEqual(self.c.complete_book_status(snapshot, result), '5 minut do końca książki.')
        self.assertEqual(self.lib.get_book(self.book['id'])['tracks'][0]['duration'], 180)

    def test_invalid_live_player_duration_uses_persisted_duration_not_probe(self):
        self.c.start(self.book['id'], index=1)
        for value in (0, None, float('nan')):
            self.c.player.duration = value
            self.assertEqual(self.c.book_status(), '5 minut do końca książki.')
        self.probe.assert_not_called()

    def test_ui_timer_status_cancel_and_worker_save_do_not_wait_for_probe(self):
        fixtures.ControllerTests.real_timer(self)
        self.c.start(self.book['id'], index=1)
        self.c.arm_timer(minutes=1, action='shutdown')
        self.paths[0].write_bytes(b'changed file')
        entered, release = threading.Event(), threading.Event()
        def slow_probe(path):
            entered.set()
            if not release.wait(5):
                raise AssertionError('probe not released')
            return 60
        self.lib._duration_probe = slow_probe
        snapshot = self.c.book_status_snapshot()
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(self.c.probe_book_status, snapshot, lambda: False)
            try:
                self.assertTrue(entered.wait(5))
                self.assertIn('aktywny.', executor.submit(self.c.timer_status).result(timeout=1))
                executor.submit(self.c.cancel_timer).result(timeout=1)
                executor.submit(self.c.save).result(timeout=1)
                self.assertIsNone(self.c.timer_state())
                self.assertTrue(self.c.player.is_playing)
            finally:
                release.set()
            result = pending.result(timeout=5)
        self.assertEqual(self.c.complete_book_status(snapshot, result), '5 minut do końca książki.')
        self.assertEqual(self.powers, [])


if __name__ == '__main__':
    unittest.main()
