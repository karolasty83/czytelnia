"""Navigation/end UX regressions; serialized controller, no NVDA/audio devices."""
import importlib.util
import unittest
from unittest.mock import Mock

from test_controller import FILE, Library, Player
from test_bookmarks_library import BookmarkFixture


def load_module(name):
    spec = importlib.util.spec_from_file_location('navigation_032_' + name, FILE.with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NavigationStatusTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library()
        self.pending, self.messages = [], []
        self.c = load_module('controller').Controller(
            self.lib, Player, self.pending.append, self.messages.append)

    def drain(self):
        while self.pending:
            self.pending.pop(0)()

    def test_manual_navigation_preserves_playback_and_persists_new_selection(self):
        for command, destination in (('next_track', 2), ('previous_track', 0)):
            for state in ('paused', 'ended', 'playing'):
                with self.subTest(command=command, state=state):
                    self.setUp()
                    self.c.start('abc', index=1)
                    self.c.player.position = 37
                    if state != 'playing':
                        self.c.toggle()
                    if state == 'ended':
                        self.c.player.position = self.c.player.duration
                    self.c.player.play = Mock(wraps=self.c.player.play)
                    getattr(self.c, command)()
                    self.assertEqual(self.c.track_index, destination)
                    self.assertEqual(self.c.player.position, 0)
                    self.assertEqual(self.c.player.is_playing, state == 'playing')
                    self.assertEqual(self.c.player.play.call_count, int(state == 'playing'))
                    self.assertEqual(self.lib.saved[-1], ('abc', destination, 0))
                    self.assertEqual(self.lib.book['track_index'], destination)
                    self.assertEqual(self.lib.book['position'], 0)
                    self.assertEqual(self.c.status(), '2 minuty do końca odcinka.')

    def test_navigation_accepts_explicit_resume_after_plugin_consumes_hold(self):
        for command in ('next_track', 'previous_track'):
            for resume in (False, True):
                with self.subTest(command=command, resume=resume):
                    self.setUp()
                    self.c.start('abc', index=1)
                    self.c.player.is_playing = not resume
                    self.c.player.play = Mock(wraps=self.c.player.play)
                    getattr(self.c, command)(resume=resume)
                    self.assertEqual(self.c.player.is_playing, resume)
                    self.assertEqual(self.c.player.play.call_count, int(resume))

    def test_navigation_uses_current_held_adjustment_intent_unless_cancelled(self):
        for state in ('active', 'cancelled', 'stale', 'done', 'no-resume'):
            with self.subTest(state=state):
                self.setUp()
                self.c.start('abc', index=1)
                session = self.c.new_adjustment('seek_relative')
                session.move(10)
                self.c.apply_adjustment(session)
                self.assertFalse(self.c.player.is_playing)
                if state == 'cancelled':
                    session.cancel()
                elif state == 'stale':
                    session.context = ('other', 1, 999)
                elif state == 'done':
                    session.done = True
                elif state == 'no-resume':
                    session.allow_resume = False
                self.c.next_track()
                self.assertEqual(self.c.player.is_playing, state == 'active')
                self.assertTrue(session.cancelled)
                self.assertEqual(self.c.track_index, 2)

    def test_pending_automatic_preload_is_not_playing_for_manual_navigation(self):
        for command, destination in (('next_track', 2), ('previous_track', 0)):
            with self.subTest(command=command):
                self.setUp()
                transitions = []
                self.c.transition = lambda *args: transitions.append(args)
                self.c.start('abc', index=0)
                self.c.player.position = 120
                self.c.player.callback('ended', None)
                self.drain()
                _, valid, resume = transitions[0]
                self.assertFalse(self.c.player.is_playing)
                self.c.player.play = Mock(wraps=self.c.player.play)
                self.c.player.callback('ended', None)
                getattr(self.c, command)()
                self.drain()
                resume()
                self.assertFalse(valid())
                self.c.player.play.assert_not_called()
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual((self.c.track_index, self.c.player.position), (destination, 0))
                self.assertEqual(self.lib.saved[-1], ('abc', destination, 0))
                self.c.toggle()
                self.assertTrue(self.c.player.is_playing)

    def test_paused_navigation_cancels_end_timers_but_retains_minutes(self):
        for mode in ('end', 'tracks', 'minutes'):
            with self.subTest(mode=mode):
                self.setUp()
                self.c.sleep_timer = load_module('sleep_timer').SleepTimer(clock=lambda: 100)
                self.c.start('abc', index=1)
                self.c.toggle()
                self.c.arm_timer(**({'minutes': 2} if mode == 'minutes' else
                                    {'tracks': 2} if mode == 'tracks' else {}))
                before = self.c.timer_state()
                self.c.next_track()
                self.assertEqual(self.c.timer_state(), before if mode == 'minutes' else None)
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.lib.saved[-1], ('abc', 2, 0))

    def test_old_eof_inside_navigation_pause_cannot_skip_destination(self):
        for playing in (False, True):
            with self.subTest(playing=playing):
                self.setUp()
                self.c.start('abc', index=1)
                self.c.player.is_playing = playing
                old_generation = self.c.generation
                arrivals = []
                pause = self.c.player.pause
                def eof_at_barrier():
                    arrivals.append(self.c.generation)
                    self.c.player.callback('ended', None)
                    self.c.player.callback('ended', None)
                    pause()
                self.c.player.pause = eof_at_barrier
                self.c.previous_track()
                self.assertEqual(arrivals, [old_generation])
                self.drain()
                self.assertEqual((self.c.track_index, self.c.player.position), (0, 0))
                self.assertEqual(self.c.player.is_playing, playing)
                self.assertEqual(self.lib.saved[-1], ('abc', 0, 0))

    def test_navigation_selection_write_failure_cannot_start_audio(self):
        self.c.start('abc', index=1)
        save = self.lib.save_position
        def fail_destination(key, index, position):
            if index == 2:
                raise OSError('destination save failed')
            save(key, index, position)
        self.lib.save_position = fail_destination
        self.c.player.play = Mock(wraps=self.c.player.play)
        self.messages.clear()
        with self.assertRaisesRegex(OSError, 'destination save failed'):
            self.c.next_track()
        self.c.player.play.assert_not_called()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.messages, [])

    def test_cancel_during_destination_save_prevents_resume_and_announcement(self):
        self.c.start('abc', index=1)
        save = self.lib.save_position
        def cancel_after_destination(key, index, position):
            save(key, index, position)
            if index == 2:
                self.c.cancelled = lambda: True
        self.lib.save_position = cancel_after_destination
        self.c.player.play = Mock(wraps=self.c.player.play)
        self.messages.clear()
        self.c.next_track()
        self.c.player.play.assert_not_called()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.lib.saved[-1], ('abc', 2, 0))

    def test_track_announcements_follow_virtual_labels_in_every_start_path(self):
        for start, index, label in ((0, 1, 'Odcinek 2'), (1, 0, 'Wstęp'),
                                    (1, 1, 'Odcinek 1'), (2, 0, 'Wstęp 1'),
                                    (2, 1, 'Wstęp 2'), (2, 2, 'Odcinek 1')):
            for action in ('start', 'bookmark-end', 'ended', 'auto-next'):
                if action == 'auto-next' and index == 0:
                    continue
                with self.subTest(start=start, index=index, action=action):
                    self.setUp()
                    self.lib.book['numbering_start'] = start
                    end_label = label.replace('Odcinek', 'odcinka').replace('Wstęp', 'wstępu')
                    expected = 'Koniec ' + end_label + '.'
                    if action == 'start':
                        self.c.start('abc', index=index)
                        expected = label + '.'
                    elif action == 'bookmark-end':
                        self.lib.get_bookmark = lambda *args: dict(track_index=index, position=120)
                        self.lib.save_bookmark_return = lambda *args: None
                        self.c.start_bookmark('abc', 'mark')
                    elif action == 'ended':
                        self.c.auto_next = lambda: False
                        self.c.start('abc', index=index)
                        self.c.player.position = 120
                        self.c.player.callback('ended', None)
                        self.drain()
                        if index == 2:
                            expected = 'Koniec książki.'
                    else:
                        transitions = []
                        self.c.transition = lambda *args: transitions.append(args)
                        self.c.start('abc', index=index - 1)
                        self.c.player.position = 120
                        self.c.player.callback('ended', None)
                        self.drain()
                        self.assertEqual(transitions[0][0], label + '.')
                        continue
                    self.assertEqual(self.messages[-1], expected)


class PersistedNavigationStatusTests(BookmarkFixture):
    def setUp(self):
        super().setUp()
        self.messages = []
        self.c = load_module('controller').Controller(
            self.lib, Player, lambda cb: cb(), self.messages.append,
            auto_next=lambda: False)

    def test_track_end_status_is_exact_not_rounded_and_resets_after_seek_or_load(self):
        self.c.start(self.key, index=1)
        self.c.toggle()
        self.c.player.position = 119.8
        self.assertEqual(self.c.status(), '0 sekund do końca odcinka.')
        self.c.seek_relative(1)
        self.assertEqual(self.c.status(), 'Koniec odcinka.')
        self.assertEqual(self.c.book_status(), '2 minuty do końca książki.')
        self.c.seek_relative(-20)
        self.assertEqual(self.c.status(), '20 sekund do końca odcinka.')
        self.c.seek_relative(20)
        self.c.player.callback('ended', None)
        self.assertEqual(self.c.status(), 'Koniec odcinka.')
        self.c.next_track()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.c.status(), '2 minuty do końca odcinka.')
        self.assertEqual(self.c.book_status(), '2 minuty do końca książki.')

    def test_intro_end_status_uses_intro_noun_without_number(self):
        self.lib.set_numbering_start(self.key, 1)
        self.c.start(self.key, index=0)
        self.c.player.position = 120
        self.c.player.callback('ended', None)
        self.assertEqual(self.c.status(), 'Koniec wstępu.')
        self.assertEqual(self.c.book_status(), '4 minuty do końca książki.')

    def test_final_book_end_status_uses_latest_exact_player_position(self):
        self.c.start(self.key, index=2)
        self.c.toggle()
        snapshot = self.c.book_status_snapshot()
        results = self.c.probe_book_status(snapshot, lambda: False)
        self.c.player.position = 119.8
        self.assertEqual(self.c.book_status(), '0 sekund do końca książki.')
        self.c.seek_relative(1)
        self.assertEqual(self.c.complete_book_status(snapshot, results), 'Koniec książki.')
        self.assertEqual(self.c.status(), 'Koniec odcinka.')
        self.c.seek_relative(-20)
        self.assertEqual(self.c.complete_book_status(snapshot, results), '20 sekund do końca książki.')
        self.c.seek_relative(20)
        self.c.player.callback('ended', None)
        self.assertEqual(self.c.book_status(), 'Koniec książki.')
        self.c.close()
        self.lib = self.reload()
        self.c = load_module('controller').Controller(
            self.lib, Player, lambda cb: cb(), self.messages.append)
        self.c.start(self.key)
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.c.status(), 'Koniec odcinka.')
        self.assertEqual(self.c.book_status(), 'Koniec książki.')
        self.c.previous_track()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.c.status(), '2 minuty do końca odcinka.')
        self.assertEqual(self.c.book_status(), '4 minuty do końca książki.')
        reloaded = self.reload().get_book(self.key)
        self.assertEqual((reloaded['track_index'], reloaded['position']), (1, 0))


if __name__ == '__main__':
    unittest.main()
