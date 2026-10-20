"""Bookmark controller integration: real Library, controlled player, no NVDA/UI."""
import unittest
from unittest.mock import patch

from test_bookmarks_library import BookmarkFixture, library, load_module
from test_controller import Player

controller = load_module('controller')
sleep_timer = load_module('sleep_timer')


class ControlledPlayer(Player):
    def __init__(self, callback):
        super().__init__(callback)
        self.calls = []
        self.on_pause = None

    def pause(self):
        self.calls.append('pause')
        super().pause()
        if self.on_pause:
            callback, self.on_pause = self.on_pause, None
            callback()

    def play(self):
        self.calls.append('play')
        super().play()

    def load(self, path, position=0, *, duration=None):
        self.calls.append(('load', path, position, duration))
        super().load(path, position=position, duration=duration)


class BookmarksControllerTests(BookmarkFixture):
    def setUp(self):
        super().setUp()
        self.pending, self.messages = [], []
        self.now = 100.0
        self.timer = sleep_timer.SleepTimer(clock=lambda: self.now)
        self.c = controller.Controller(self.lib, ControlledPlayer, self.pending.append,
                                       self.messages.append, sleep_timer=self.timer)

    def drain(self):
        while self.pending:
            self.pending.pop(0)()

    def test_add_uses_loaded_playing_and_paused_position_without_playback_side_effect(self):
        self.lib.save_position(self.key, 2, 99)
        self.c.start(self.key, index=1)
        for playing in (True, False):
            with self.subTest(playing=playing):
                self.c.player.is_playing = playing
                self.c.player.position = 12.75 if playing else 33.5
                self.c.arm_timer(minutes=2)
                self.messages.clear()
                before = (self.c.generation, self.c.adjustment_context,
                          list(self.c.player.calls), self.c.timer_state(), self.progress())
                mark = self.c.add_bookmark()
                self.assertEqual((mark['track_index'], mark['position']),
                                 (1, self.c.player.position))
                self.assertEqual(self.lib.get_bookmark(self.key, mark['id']), mark)
                self.assertEqual((self.c.generation, self.c.adjustment_context,
                                  self.c.player.calls, self.c.timer_state(), self.progress()), before)
                self.assertEqual(self.c.player.is_playing, playing)
                self.assertEqual(self.messages, ['Dodano zakładkę: {0}.'.format(mark['name'])])
        self.assertEqual(len(self.reload().bookmarks(self.key)), 2)

    def test_add_without_loaded_book_notifies_without_using_library_selection(self):
        before = self.lib.json_path.read_bytes()
        self.assertIsNone(self.c.add_bookmark())
        self.assertEqual(self.messages, ['Najpierw uruchom książkę.'])
        self.assertEqual(self.c.player.calls, [])
        self.assertEqual(self.lib.json_path.read_bytes(), before)

    def test_start_bookmark_jumps_backwards_then_other_track_and_book(self):
        first = self.lib.add_bookmark(self.key, 1, 12.25)
        second = self.lib.add_bookmark(self.key, 0, 7.5)
        self.c.start(self.key, index=1)
        self.c.player.position = 95
        self.c.player.speed = 1.5
        for mark in (first, second):
            with self.subTest(mark=mark):
                old_generation = self.c.generation
                self.c.start_bookmark(self.key, mark['id'])
                self.assertEqual((self.c.track_index, self.c.player.position),
                                 (mark['track_index'], mark['position']))
                self.assertTrue(self.c.player.is_playing)
                self.assertGreater(self.c.generation, old_generation)
                self.assertEqual(self.c.player.speed, 1.5)
                self.assertEqual(self.lib.get_bookmark(self.key, mark['id']), mark)
        other = self.root / 'other'
        other.mkdir()
        (other / '1.wav').write_bytes(b'fixture')
        other_key = self.lib.import_book(other)['id']
        other_mark = self.lib.add_bookmark(other_key, 0, 22.75)
        self.lib.save_position(other_key, 0, 88)
        self.c.player.position = 66
        token = self.c.book_status_snapshot()['token']
        self.c.start_bookmark(other_key, other_mark['id'])
        self.assertEqual((self.c.book_id, self.c.track_index, self.c.player.position),
                         (other_key, 0, 22.75))
        self.assertEqual(self.progress(), (0, 66))
        self.assertTrue(token.is_set())
        self.assertEqual(self.c.player.speed, 1.5)
        self.c.close()
        self.lib = self.reload()
        book = self.lib.get_book(other_key)
        self.assertEqual((book['track_index'], book['position']), (0, 22.75))
        self.assertEqual(self.lib.get_bookmark(other_key, other_mark['id']), other_mark)

    def test_start_from_bookmark_without_loaded_book_ignores_saved_progress(self):
        mark = self.lib.add_bookmark(self.key, 0, 3.25)
        self.lib.save_position(self.key, 2, 115)
        self.c.start_bookmark(self.key, mark['id'])
        self.assertEqual((self.c.book_id, self.c.track_index, self.c.player.position),
                         (self.key, 0, 3.25))
        self.assertTrue(self.c.player.is_playing)

    def test_bookmark_at_track_end_does_not_rewind_or_skip_to_next_track(self):
        mark = self.lib.add_bookmark(self.key, 0, 120)
        # The real backend rewinds an ended track on play; playing must be avoided.
        original = self.c.player.play
        def rewind_on_play():
            if self.c.player.position >= self.c.player.duration:
                self.c.player.position = 0
            original()
        self.c.player.play = rewind_on_play
        self.c.start_bookmark(self.key, mark['id'])
        self.assertEqual((self.c.track_index, self.c.player.position), (0, 120))
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.messages, ['Koniec odcinka 1.'])

    def test_closed_or_cancelled_controller_does_not_add_or_start_bookmark(self):
        mark = self.lib.add_bookmark(self.key, 0, 12)
        self.c.start(self.key, index=0)
        for state in ('closed', 'cancelled'):
            with self.subTest(state=state):
                self.c.closed = state == 'closed'
                self.c.cancelled = lambda: state == 'cancelled'
                before = (self.lib.json_path.read_bytes(), list(self.c.player.calls), self.c.generation)
                self.messages.clear()
                self.assertIsNone(self.c.add_bookmark())
                self.assertIsNone(self.c.start_bookmark(self.key, mark['id']))
                self.assertEqual((self.lib.json_path.read_bytes(), self.c.player.calls, self.c.generation), before)
                self.assertEqual(self.messages, [])

    def test_jump_cancels_old_eof_transition_and_adjustment_but_keeps_same_track_timer(self):
        self.c.start(self.key, index=0)
        mark = self.lib.add_bookmark(self.key, 0, 2)
        self.c.arm_timer(tracks=2)
        timer_before = self.c.timer_state()
        session = self.c.new_adjustment('seek_relative')
        self.c._adjustment = session
        self.c._pending_transition = self.c.generation
        generation = self.c.generation
        arrivals = []
        def old_eof():
            arrivals.append(self.c.generation)
            self.c.player.callback('ended', None)
        self.c.player.on_pause = old_eof
        self.c.start_bookmark(self.key, mark['id'])
        self.assertEqual(arrivals, [generation])
        self.assertTrue(session.cancelled)
        self.assertIsNone(self.c._pending_transition)
        self.assertIsNone(self.c._adjustment)
        self.drain()
        self.assertEqual((self.c.track_index, self.c.player.position), (0, 2))
        self.assertEqual(self.c.timer_state(), timer_before)
        self.assertTrue(self.c.player.is_playing)

    def test_other_track_jump_cancels_end_timers_and_preserves_minutes_timer(self):
        mark = self.lib.add_bookmark(self.key, 1, 21)
        for mode in ('end', 'tracks', 'minutes'):
            with self.subTest(mode=mode):
                self.c.start(self.key, index=0)
                self.c.arm_timer(**({'minutes': 2} if mode == 'minutes' else
                                    {'tracks': 2} if mode == 'tracks' else {}))
                before = self.c.timer_state()
                self.c.player.pause()
                self.c.start_bookmark(self.key, mark['id'])
                self.assertEqual(self.c.timer_state(), before if mode == 'minutes' else None)
                self.assertEqual((self.c.track_index, self.c.player.position), (1, 21))
                self.assertTrue(self.c.player.is_playing)

    def test_add_write_failure_does_not_notify_success_or_change_playback(self):
        self.c.start(self.key, index=1)
        self.c.player.position = 25
        self.messages.clear()
        before = (self.lib.books(), self.c.generation, list(self.c.player.calls))
        with patch.object(library.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(library.LibraryError):
                self.c.add_bookmark()
        self.assertEqual((self.lib.books(), self.c.generation, self.c.player.calls), before)
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.messages, [])

    def test_unknown_bookmark_is_rejected_before_changing_playback(self):
        self.c.start(self.key, index=1)
        before = (self.lib.json_path.read_bytes(), self.c.generation, list(self.c.player.calls))
        with self.assertRaises(library.LibraryError):
            self.c.start_bookmark(self.key, 'f' * 32)
        self.assertEqual((self.lib.json_path.read_bytes(), self.c.generation, self.c.player.calls), before)
        self.assertTrue(self.c.player.is_playing)

    def test_browse_rename_and_remove_do_not_touch_loaded_playback_or_progress(self):
        self.c.start(self.key, index=1)
        self.c.player.position = 50
        mark = self.c.add_bookmark()
        before = (self.progress(), self.c.generation, list(self.c.player.calls), self.c.player.position)
        self.lib.bookmarks(self.key)
        self.lib.get_bookmark(self.key, mark['id'])
        self.lib.rename_bookmark(self.key, mark['id'], 'Chapter')
        self.lib.remove_bookmark(self.key, mark['id'])
        self.assertEqual((self.progress(), self.c.generation, self.c.player.calls, self.c.player.position), before)
        self.assertTrue(self.c.player.is_playing)


if __name__ == '__main__':
    unittest.main()
