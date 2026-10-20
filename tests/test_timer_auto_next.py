"""Count timers temporarily enable auto-next without changing preferences."""
import importlib.util
import unittest

import test_controller as fixtures


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, fixtures.FILE.with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load_module('timer_auto_next_controller', 'controller.py')
sleep_timer = load_module('timer_auto_next_sleep', 'sleep_timer.py')


class TimerAutoNextTests(unittest.TestCase):
    def setUp(self):
        self.lib = fixtures.Library()
        self.pending, self.messages, self.transitions, self.powers = [], [], [], []
        self.preference = False
        self.now = 100.0
        self.base_auto_next = lambda: self.preference
        self.c = controller.Controller(
            self.lib, fixtures.Player, self.pending.append, self.messages.append,
            auto_next=self.base_auto_next,
            sleep_timer=sleep_timer.SleepTimer(clock=lambda: self.now),
            power=self.powers.append)
        self.c.start('abc', index=0)

    def drain(self):
        while self.pending:
            self.pending.pop(0)()

    def natural_end(self):
        self.c.player.position = self.c.player.duration
        self.c.player.is_playing = False
        self.c.player.callback('ended', None)
        self.c.player.callback('ended', None)
        self.drain()

    def test_count_runs_sequentially_with_base_off_and_stops_at_final_eof(self):
        for delayed in (False, True):
            for action in ('stop', 'sleep', 'shutdown'):
                with self.subTest(delayed=delayed, action=action):
                    self.setUp()
                    if delayed:
                        self.c.transition = lambda *args: self.transitions.append(args)
                    self.c.arm_timer(tracks=2, action=action)
                    self.natural_end()
                    self.assertEqual(self.c.track_index, 1)
                    self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
                    self.assertEqual(self.powers, [])
                    if delayed:
                        self.assertEqual(len(self.transitions), 1)
                        _, valid, resume = self.transitions[0]
                        self.assertTrue(valid())
                        self.assertFalse(self.c.player.is_playing)
                        resume()
                        resume()  # a transition is one-shot
                    self.assertTrue(self.c.player.is_playing)
                    self.assertTrue(self.c.effective_auto_next())
                    self.assertIs(self.c.auto_next, self.base_auto_next)
                    self.assertFalse(self.preference)
                    self.natural_end()
                    self.assertEqual(self.c.track_index, 1)
                    self.assertEqual(len(self.c.player.loaded), 2)
                    self.assertFalse(self.c.player.is_playing)
                    self.assertIsNone(self.c.timer_state())
                    self.assertFalse(self.c.effective_auto_next())
                    self.assertFalse(self.c.check_timer())
                    self.assertEqual(self.powers, [] if action == 'stop' else [action])
                    self.assertEqual(self.lib.saved[-1], ('abc', 1, 120))

    def test_explicit_toggle_off_cancels_count_even_when_preference_already_off(self):
        for pending_transition in (False, True):
            with self.subTest(pending_transition=pending_transition):
                self.setUp()
                self.c.arm_timer(tracks=2)
                if pending_transition:
                    self.c.transition = lambda *args: self.transitions.append(args)
                    self.natural_end()
                    _, valid, resume = self.transitions[0]
                    self.assertTrue(valid())
                self.c.auto_next_changed(False)
                self.assertIsNone(self.c.timer_state())
                self.assertFalse(self.c.effective_auto_next())
                self.assertFalse(self.preference)
                if pending_transition:
                    self.assertIsNone(self.c._pending_transition)
                    self.assertFalse(valid())
                    resume()
                    self.assertFalse(self.c.player.is_playing)
                else:
                    self.assertTrue(self.c.player.is_playing)
                    self.natural_end()
                    self.assertEqual(self.c.track_index, 0)
                    self.assertFalse(self.c.player.is_playing)

    def test_count_confirmation_explains_temporary_auto_next_only_when_base_off(self):
        self.c.arm_timer(tracks=2)
        self.assertEqual(self.messages[-1], 'Wyłącznik czasowy włączony. '
                         'Automatyczne przechodzenie do następnego odcinka '
                         'włączone tymczasowo na czas wyłącznika.')
        self.assertFalse(self.preference)
        self.preference = True
        self.c.arm_timer(tracks=2)
        self.assertEqual(self.messages[-1], 'Wyłącznik czasowy włączony.')
        self.preference = False
        for options in ({'minutes': 1}, {}):
            self.c.arm_timer(**options)
            self.assertEqual(self.messages[-1], 'Wyłącznik czasowy włączony.')

    def test_count_teardown_restores_live_base_preference(self):
        for base in (False, True):
            for operation in ('cancel', 'minutes', 'end', 'next', 'previous',
                              'other_book', 'remove', 'close'):
                with self.subTest(base=base, operation=operation):
                    self.setUp()
                    self.preference = base
                    self.c.start('abc', index=1)
                    self.c.arm_timer(tracks=2)
                    self.assertTrue(self.c.effective_auto_next())
                    operations = {
                        'cancel': self.c.cancel_timer,
                        'minutes': lambda: self.c.arm_timer(minutes=1),
                        'end': self.c.arm_timer,
                        'next': self.c.next_track,
                        'previous': self.c.previous_track,
                        'other_book': lambda: self.c.start('other', index=0),
                        'remove': lambda: self.c.remove('abc'),
                        'close': self.c.close,
                    }
                    operations[operation]()
                    self.assertEqual(self.c.effective_auto_next(), base)
                    self.assertEqual(self.preference, base)
                    self.assertIs(self.c.auto_next, self.base_auto_next)
                    if operation in ('minutes', 'end'):
                        self.assertEqual(self.c.timer_state()['mode'], operation)
                    else:
                        self.assertIsNone(self.c.timer_state())

    def test_base_on_survives_count_completion_without_advancing_past_timer(self):
        self.preference = True
        self.c.arm_timer(tracks=1)
        self.natural_end()
        self.assertIsNone(self.c.timer_state())
        self.assertTrue(self.c.effective_auto_next())
        self.assertTrue(self.preference)
        self.assertEqual(self.c.track_index, 0)
        self.assertEqual(len(self.c.player.loaded), 1)
        self.assertFalse(self.c.player.is_playing)

    def test_toggle_off_does_not_cancel_end_or_minute_timers(self):
        for options in ({'minutes': 1}, {}):
            with self.subTest(options=options):
                self.setUp()
                self.c.arm_timer(action='shutdown', **options)
                state = self.c.timer_state()
                self.c.auto_next_changed(False)
                self.assertEqual(self.c.timer_state(), state)
                self.assertFalse(self.c.effective_auto_next())
                if options:
                    self.now += 60
                    self.assertTrue(self.c.check_timer())
                else:
                    self.natural_end()
                self.assertEqual(self.powers, ['shutdown'])
                self.assertIsNone(self.c.timer_state())

    def test_toggle_off_cancels_pending_transition_but_preserves_non_count_timer(self):
        for options in ({'minutes': 1}, {}):
            with self.subTest(options=options):
                self.setUp()
                self.preference = True
                self.c.transition = lambda *args: self.transitions.append(args)
                self.natural_end()
                _, valid, resume = self.transitions[0]
                self.c.arm_timer(**options)
                state = self.c.timer_state()
                self.preference = False
                self.c.auto_next_changed(False)
                self.assertEqual(self.c.timer_state(), state)
                self.assertFalse(valid())
                resume()
                self.assertFalse(self.c.player.is_playing)

    def test_cancel_or_replace_count_invalidates_delayed_resume_with_base_off(self):
        for operation in ('cancel', 'minutes', 'end'):
            with self.subTest(operation=operation):
                self.setUp()
                self.c.transition = lambda *args: self.transitions.append(args)
                self.c.arm_timer(tracks=2)
                self.natural_end()
                _, valid, resume = self.transitions[0]
                if operation == 'cancel':
                    self.c.cancel_timer()
                else:
                    self.c.arm_timer(**({'minutes': 1} if operation == 'minutes' else {}))
                self.assertFalse(valid())
                resume()
                self.assertFalse(self.c.player.is_playing)
                self.assertFalse(self.c.effective_auto_next())

    def test_replacing_count_uses_new_count_and_never_writes_base(self):
        self.c.arm_timer(tracks=3)
        self.c.arm_timer(tracks=1)
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
        self.natural_end()
        self.assertIsNone(self.c.timer_state())
        self.assertEqual(self.c.track_index, 0)
        self.assertFalse(self.c.effective_auto_next())
        self.assertFalse(self.preference)

    def test_temporary_auto_next_is_bound_to_target_book(self):
        self.c.sleep_timer.arm(tracks=2, target=('other', 0))
        self.assertFalse(self.c.effective_auto_next())
        self.natural_end()
        self.assertEqual(self.c.track_index, 0)
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 2)
        self.assertFalse(self.c.player.is_playing)

    def test_seek_to_duration_is_not_a_counted_natural_end(self):
        self.c.arm_timer(tracks=2)
        self.c.seek_relative(120)
        self.c.toggle()
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 2)
        self.assertEqual(self.powers, [])

    def test_explicit_skip_cancels_timer_without_counting_queued_eof(self):
        self.c.arm_timer(tracks=1, action='shutdown')
        self.c.player.position = self.c.player.duration
        self.c.player.callback('ended', None)
        self.c.next_track()
        self.drain()
        self.assertEqual(self.c.track_index, 1)
        self.assertTrue(self.c.player.is_playing)
        self.assertIsNone(self.c.timer_state())
        self.assertFalse(self.c.effective_auto_next())
        self.assertEqual(self.powers, [])

    def test_toggle_on_keeps_count_and_updates_effective_base_after_cancel(self):
        self.c.arm_timer(tracks=2)
        state = self.c.timer_state()
        self.preference = True
        self.c.auto_next_changed(True)
        self.assertEqual(self.c.timer_state(), state)
        self.c.cancel_timer()
        self.assertTrue(self.c.effective_auto_next())
        self.assertTrue(self.preference)


if __name__ == '__main__':
    unittest.main()
