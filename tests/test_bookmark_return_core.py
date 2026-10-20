"""Persistent bookmark excursions: real Library, controlled audio, no UI."""
import json
from copy import deepcopy
import unittest
from unittest.mock import patch

from test_bookmarks_library import BookmarkFixture, library
from test_bookmarks_controller import ControlledPlayer, controller


class ReturnLibraryTests(BookmarkFixture):
    def test_first_point_is_persistent_detached_and_separate_from_progress(self):
        self.lib.save_position(self.key, 2, 90)
        before = self.lib.json_path.read_bytes()
        self.assertIsNone(self.lib.get_bookmark_return(self.key))
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        point = self.lib.save_bookmark_return(self.key, 1, 32.75)
        self.assertEqual(point, dict(track_index=1, position=32.75))
        point['position'] = 999
        first_bytes = self.lib.json_path.read_bytes()
        self.assertEqual(self.lib.save_bookmark_return(self.key, 0, 8),
                         dict(track_index=1, position=32.75))
        self.assertEqual(self.lib.json_path.read_bytes(), first_bytes)
        self.assertEqual(self.progress(), (2, 90))
        self.lib = self.reload()
        point = self.lib.get_bookmark_return(self.key)
        self.assertEqual(point, dict(track_index=1, position=32.75))
        point.clear()
        self.assertEqual(self.lib.get_bookmark_return(self.key),
                         dict(track_index=1, position=32.75))

    def test_optional_stored_point_has_strict_shape_and_position_validation(self):
        valid = json.loads(self.lib.json_path.read_bytes())
        bad = [None, [], '', True, {}, {'track_index': 0}, {'position': 1},
               dict(track_index=0, position=1, source='unwanted'),
               dict(track_index=0, position=1, power='shutdown')]
        for field, values in {
            'track_index': [None, -1, 3, True, 0.0, '0'],
            'position': [None, -1, True, '1', float('nan'), float('inf'), 10 ** 1000],
        }.items():
            bad.extend(dict(dict(track_index=0, position=1), **{field: value}) for value in values)
        for value in bad:
            with self.subTest(value=value):
                document = deepcopy(valid)
                document['books'][0]['bookmark_return'] = value
                data = json.dumps(document).encode('utf-8')
                self.lib.json_path.write_bytes(data)
                with self.assertRaises(library.LibraryError):
                    self.reload()
                self.assertEqual(self.lib.json_path.read_bytes(), data)

    def test_completion_atomically_restores_progress_and_clears_point(self):
        before = self.lib.json_path.read_bytes()
        self.assertIsNone(self.lib.complete_bookmark_return(self.key))
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.lib.save_bookmark_return(self.key, 1, 42.5)
        self.lib.save_position(self.key, 2, 7)
        before, books = self.lib.json_path.read_bytes(), self.lib.books()
        for failure in ('replace', 'fsync'):
            with self.subTest(failure=failure):
                with patch.object(library.os, failure, side_effect=OSError('disk full')):
                    with self.assertRaises(library.LibraryError):
                        self.lib.complete_bookmark_return(self.key)
                self.assertEqual(self.lib.books(), books)
                self.assertEqual(self.lib.json_path.read_bytes(), before)
                self.assertEqual(list(self.lib.addons_dir.glob('.czytelnia-*.tmp')), [])
        self.lib.complete_bookmark_return(self.key)
        self.lib = self.reload()
        self.assertEqual(self.progress(), (1, 42.5))
        self.assertIsNone(self.lib.get_bookmark_return(self.key))
        self.assertNotIn('bookmark_return', self.lib.get_book(self.key))
        self.assertEqual(self.lib.save_bookmark_return(self.key, 0, 88),
                         dict(track_index=0, position=88.0))

    def test_save_rejects_invalid_positions_and_failed_atomic_write_without_changes(self):
        before, books = self.lib.json_path.read_bytes(), self.lib.books()
        for track, position in [(True, 0), (-1, 0), (3, 0), (0.0, 1), ('0', 1),
                                (0, True), (0, -1), (0, float('nan')), (0, float('inf')),
                                (0, '1'), (0, None), (0, 10 ** 1000)]:
            with self.subTest(track=track, position=position), self.assertRaises(library.LibraryError):
                self.lib.save_bookmark_return(self.key, track, position)
        for failure in ('replace', 'fsync'):
            with patch.object(library.os, failure, side_effect=OSError('disk full')):
                with self.assertRaises(library.LibraryError):
                    self.lib.save_bookmark_return(self.key, 1, 25)
            self.assertEqual(list(self.lib.addons_dir.glob('.czytelnia-*.tmp')), [])
        self.assertEqual(self.lib.books(), books)
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.assertIsNone(self.reload().get_bookmark_return(self.key))

    def test_external_changes_are_preserved_for_both_return_writes(self):
        self.lib.save_bookmark_return(self.key, 1, 25)
        books = self.lib.books()
        original = self.lib.json_path.read_bytes()
        self.lib.json_path.write_bytes(b'{corrupt external edit')
        with self.assertRaises(library.LibraryError):
            self.lib.complete_bookmark_return(self.key)
        self.assertEqual(self.lib.books(), books)
        self.assertEqual(self.lib.json_path.read_bytes(), b'{corrupt external edit')
        # Remove the anchor to exercise capture against another external edit.
        self.lib.json_path.write_bytes(original)
        self.lib.complete_bookmark_return(self.key)
        books = self.lib.books()
        self.lib.json_path.write_bytes(b'{another external edit')
        with self.assertRaises(library.LibraryError):
            self.lib.save_bookmark_return(self.key, 0, 1)
        self.assertEqual(self.lib.books(), books)
        self.assertEqual(self.lib.json_path.read_bytes(), b'{another external edit')

    def test_remove_reimport_clears_anchor_and_invalid_ids_are_rejected(self):
        self.lib.save_bookmark_return(self.key, 1, 25)
        for key in (None, '../bad', 'f' * 32):
            for operation in (lambda: self.lib.get_bookmark_return(key),
                              lambda: self.lib.save_bookmark_return(key, 0, 1),
                              lambda: self.lib.complete_bookmark_return(key)):
                with self.subTest(key=key), self.assertRaises(library.LibraryError):
                    operation()
        self.lib.remove_book(self.key)
        self.lib = self.reload()
        new_key = self.lib.import_book(self.source)['id']
        self.assertNotEqual(new_key, self.key)
        self.assertIsNone(self.lib.get_bookmark_return(new_key))
        self.assertNotIn('bookmark_return', self.lib.get_book(new_key))


class ReturnControllerTests(BookmarkFixture):
    def setUp(self):
        super().setUp()
        self.pending, self.messages = [], []
        self.c = controller.Controller(self.lib, ControlledPlayer, self.pending.append,
                                       self.messages.append)

    def test_first_jump_captures_live_clock_after_barrier_and_keeps_first_anchor(self):
        first = self.lib.add_bookmark(self.key, 0, 3)
        second = self.lib.add_bookmark(self.key, 1, 14)
        self.lib.save_position(self.key, 2, 4)
        self.c.start(self.key, index=2)
        self.c.player.position = 50
        self.c.player.on_pause = lambda: setattr(self.c.player, 'position', 51.25)
        self.c.start_bookmark(self.key, first['id'])
        expected = dict(track_index=2, position=51.25)
        self.assertEqual(self.lib.get_bookmark_return(self.key), expected)
        self.assertEqual((self.c.track_index, self.c.player.position), (0, 3))
        self.c.player.position = 9
        self.c.start_bookmark(self.key, second['id'])
        self.assertEqual(self.reload().get_bookmark_return(self.key), expected)
        self.assertEqual((self.c.track_index, self.c.player.position), (1, 14))
        self.assertEqual(self.lib.bookmarks(self.key), [first, second])

    def test_successful_return_restores_progress_consumes_point_and_allows_new_excursion(self):
        mark = self.lib.add_bookmark(self.key, 0, 3)
        self.c.start(self.key, index=2)
        self.c.player.position = 65.75
        self.c.player.speed = 1.5
        self.c.start_bookmark(self.key, mark['id'])
        self.assertIs(self.c.return_from_bookmark(self.key), True)
        self.assertEqual((self.c.track_index, self.c.player.position), (2, 65.75))
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.c.player.speed, 1.5)
        self.assertEqual(self.progress(), (2, 65.75))
        self.assertIsNone(self.reload().get_bookmark_return(self.key))
        self.assertEqual(self.messages[-1], 'Powrót do słuchania.')
        self.c.player.position = 72
        self.c.start_bookmark(self.key, mark['id'])
        self.assertEqual(self.reload().get_bookmark_return(self.key),
                         dict(track_index=2, position=72.0))

    def test_return_without_anchor_has_no_playback_or_progress_side_effects(self):
        self.c.start(self.key, index=1)
        self.c.player.position = 19
        self.messages.clear()
        before = (self.lib.json_path.read_bytes(), list(self.c.player.calls),
                  self.c.generation, self.c.adjustment_context)
        self.assertIs(self.c.return_from_bookmark(self.key), False)
        self.assertEqual((self.lib.json_path.read_bytes(), self.c.player.calls,
                          self.c.generation, self.c.adjustment_context), before)
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.messages, [])

    def test_cancelled_or_closed_during_load_retains_point_without_success(self):
        for state in ('closed', 'cancelled'):
            with self.subTest(state=state):
                c = controller.Controller(self.lib, ControlledPlayer, self.pending.append,
                                          self.messages.append)
                self.lib.save_bookmark_return(self.key, 2, 71)
                load = c.player.load
                def cancel_load(*args, **kwargs):
                    load(*args, **kwargs)
                    c.closed = state == 'closed'
                    c.cancelled = lambda: state == 'cancelled'
                c.player.load = cancel_load
                self.messages.clear()
                self.assertIs(c.return_from_bookmark(self.key), False)
                self.assertEqual(self.reload().get_bookmark_return(self.key),
                                 dict(track_index=2, position=71.0))
                self.assertNotIn('play', c.player.calls)
                self.assertNotIn('Powrót do słuchania.', self.messages)

    def test_restart_return_and_independent_books_use_target_progress(self):
        first = self.lib.add_bookmark(self.key, 0, 3)
        self.lib.save_position(self.key, 2, 90)
        self.c.start_bookmark(self.key, first['id'])  # no loaded book
        self.assertEqual(self.lib.get_bookmark_return(self.key), dict(track_index=2, position=90))
        other_source = self.root / 'other'
        other_source.mkdir()
        (other_source / '1.wav').write_bytes(b'fixture')
        other = self.lib.import_book(other_source)['id']
        self.lib.save_position(other, 0, 77.5)
        second = self.lib.add_bookmark(other, 0, 8)
        self.c.player.position = 11
        self.c.start_bookmark(other, second['id'])
        self.assertEqual(self.lib.get_bookmark_return(other), dict(track_index=0, position=77.5))
        self.assertEqual(self.lib.get_bookmark_return(self.key), dict(track_index=2, position=90))
        self.c.close()
        self.lib = self.reload()
        self.c = controller.Controller(self.lib, ControlledPlayer, self.pending.append, self.messages.append)
        self.assertTrue(self.c.return_from_bookmark(self.key))
        self.assertEqual((self.c.book_id, self.c.track_index, self.c.player.position), (self.key, 2, 90))
        self.c.player.speed = 1.75
        self.assertTrue(self.c.return_from_bookmark(other))
        self.assertEqual((self.c.book_id, self.c.track_index, self.c.player.position), (other, 0, 77.5))
        self.assertEqual(self.c.player.speed, 1.75)
        self.assertIsNone(self.reload().get_bookmark_return(self.key))
        self.assertIsNone(self.reload().get_bookmark_return(other))

    def test_anchor_failure_prevents_jump_and_unknown_mark_does_not_pause(self):
        mark = self.lib.add_bookmark(self.key, 0, 3)
        self.c.start(self.key, index=2)
        self.c.player.position = 70
        calls = list(self.c.player.calls)
        before = self.lib.json_path.read_bytes()
        with self.assertRaises(library.LibraryError):
            self.c.start_bookmark(self.key, 'f' * 32)
        self.assertEqual(self.c.player.calls, calls)
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.messages.clear()
        with patch.object(library.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(library.LibraryError):
                self.c.start_bookmark(self.key, mark['id'])
        self.assertEqual(self.c.player.calls, calls + ['pause'])
        self.assertEqual((self.c.track_index, self.c.player.position), (2, 70))
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        self.assertIsNone(self.lib.get_bookmark_return(self.key))
        self.assertEqual(self.messages, [])

    def test_failed_jump_after_capture_keeps_anchor_for_retry(self):
        mark = self.lib.add_bookmark(self.key, 0, 3)
        self.c.start(self.key, index=2)
        self.c.player.position = 70
        with patch.object(self.c.player, 'load', side_effect=RuntimeError('load failed')):
            with self.assertRaisesRegex(RuntimeError, 'load failed'):
                self.c.start_bookmark(self.key, mark['id'])
        self.assertEqual(self.reload().get_bookmark_return(self.key), dict(track_index=2, position=70))
        self.c.start_bookmark(self.key, mark['id'])
        self.assertTrue(self.c.return_from_bookmark(self.key))
        self.assertEqual((self.c.track_index, self.c.player.position), (2, 70))

    def test_return_failures_keep_anchor_and_do_not_announce_success(self):
        self.lib.save_bookmark_return(self.key, 2, 70)
        self.c.start(self.key, index=0)
        stages = [(self.lib, 'track_duration'), (self.lib, 'track_path'),
                  (self.lib, 'save_position'), (self.c.player, 'load'), (self.c.player, 'play'),
                  (self.lib, 'complete_bookmark_return')]
        for target, method in stages:
            with self.subTest(method=method):
                self.messages.clear()
                with patch.object(target, method, side_effect=RuntimeError(method)):
                    with self.assertRaisesRegex(RuntimeError, method):
                        self.c.return_from_bookmark(self.key)
                self.assertEqual(self.reload().get_bookmark_return(self.key),
                                 dict(track_index=2, position=70))
                self.assertNotIn('Powrót do słuchania.', self.messages)
        self.assertTrue(self.c.player.is_playing)  # save failed AFTER playback started
        self.assertTrue(self.c.return_from_bookmark(self.key))
        self.assertIsNone(self.reload().get_bookmark_return(self.key))

    def test_real_completion_disk_failure_retains_anchor_after_audio_started(self):
        self.lib.save_bookmark_return(self.key, 2, 70)
        self.c.start(self.key, index=0)
        replace = library.os.replace
        def fail_completion(source, destination):
            payload = json.loads(library.Path(source).read_bytes())
            if 'bookmark_return' not in payload['books'][0]:
                raise OSError('completion disk full')
            return replace(source, destination)
        self.messages.clear()
        with patch.object(library.os, 'replace', side_effect=fail_completion):
            with self.assertRaises(library.LibraryError):
                self.c.return_from_bookmark(self.key)
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.reload().get_bookmark_return(self.key), dict(track_index=2, position=70))
        self.assertNotIn('Powrót do słuchania.', self.messages)
        self.assertTrue(self.c.return_from_bookmark(self.key))

    def test_end_position_return_never_rewinds_or_advances(self):
        self.lib.save_bookmark_return(self.key, 0, 120)
        self.c.start(self.key, index=2)
        self.c.player.calls.clear()
        self.assertTrue(self.c.return_from_bookmark(self.key))
        self.assertEqual((self.c.track_index, self.c.player.position), (0, 120))
        self.assertNotIn('play', self.c.player.calls)
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.progress(), (0, 120))
        self.assertIsNone(self.reload().get_bookmark_return(self.key))

    def test_closed_cancelled_return_is_noop_even_with_anchor(self):
        self.lib.save_bookmark_return(self.key, 1, 50)
        before = self.lib.json_path.read_bytes()
        for state in ('closed', 'cancelled'):
            with self.subTest(state=state):
                self.c.closed = state == 'closed'
                self.c.cancelled = lambda: state == 'cancelled'
                self.assertIs(self.c.return_from_bookmark(self.key), False)
                self.assertEqual(self.c.player.calls, [])
                self.assertEqual(self.lib.json_path.read_bytes(), before)
                self.assertEqual(self.messages, [])


if __name__ == '__main__':
    unittest.main()
