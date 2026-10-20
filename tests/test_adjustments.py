"""Held adjustment contract: no per-key restart and no stale work."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
from test_controller import Library, Player, Timer

FILE = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/controller.py'


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('adjust_controller_test', FILE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.lib, self.events, self.messages = Library(), [], []
        self.c = m.Controller(self.lib, Player, self.events.append, self.messages.append)
        self.c.start('abc', 0)
        self.c.player.duration = 600
        self.c.player.position = 100
        self.messages.clear()

    def session(self, name):
        self.assertTrue(hasattr(self.c, 'new_adjustment'), 'Brak gestu suwakowego')
        return self.c.new_adjustment(name)

    def test_seek_moves_while_held_without_restarts_and_resumes_once_on_release(self):
        s = self.session('seek_relative')
        play = self.c.player.play = Mock(wraps=self.c.player.play)
        pause = self.c.player.pause = Mock(wraps=self.c.player.pause)
        saves_before = len(self.lib.saved)
        for _ in range(10):
            s.move(15)
            self.c.apply_adjustment(s)
        self.assertEqual(self.c.player.position, 250)
        self.assertFalse(self.c.player.is_playing)
        pause.assert_called_once()
        play.assert_not_called()
        self.assertEqual(len(self.lib.saved), saves_before)
        s.release()
        self.c.apply_adjustment(s)
        play.assert_called_once()
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 0, 250))
        self.assertEqual(self.messages, [])
        self.c.apply_adjustment(s)
        play.assert_called_once()
        self.assertFalse(s.cancelled, 'Repeated completion must stay successful')


    def test_speed_floor_is_silent_noop_and_reversal_still_works(self):
        s = self.session('change_speed')
        generation = self.c.generation
        before = list(self.lib.saved)
        pause = self.c.player.pause = Mock(wraps=self.c.player.pause)
        play = self.c.player.play = Mock(wraps=self.c.player.play)
        for _ in range(3):
            s.move(-.5)
            self.assertIsNone(self.c.apply_adjustment(s))
        self.assertEqual(self.c.player.speed, 1)
        self.assertEqual(s.target, 1)
        pause.assert_not_called()
        play.assert_not_called()
        self.assertEqual(self.c.generation, generation)
        self.assertEqual(self.lib.saved, before)
        s.move(.5)
        s.release()
        self.assertEqual(self.c.apply_adjustment(s), 'Prędkość 1,5×')
        self.assertEqual(self.c.player.speed, 1.5)
        pause.assert_called_once()
        play.assert_called_once()
        self.assertEqual(self.messages, [])

    def test_released_speed_floor_noop_preserves_pending_natural_eof(self):
        s = self.session('change_speed')
        s.move(-.5)
        s.release()
        self.c.player.position = self.c.player.duration
        self.c.player.is_playing = False
        self.c.player.callback('ended', None)
        generation = self.c.generation
        pause = self.c.player.pause = Mock(wraps=self.c.player.pause)
        self.assertIsNone(self.c.apply_adjustment(s))
        self.assertTrue(s.done)
        self.assertEqual(self.c.generation, generation)
        pause.assert_not_called()
        while self.events:
            self.events.pop(0)()
        self.assertEqual(self.c.track_index, 1)
        self.assertTrue(self.c.player.is_playing)

    def test_speed_lower_bound_is_one_after_decrease_from_upper_bound(self):
        self.c.player.speed = 2
        s = self.session('change_speed')
        s.move(-10)
        s.release()
        self.assertEqual(self.c.apply_adjustment(s), 'Prędkość 1×')
        self.assertEqual(self.c.player.speed, 1)
        self.c.change_speed(10)
        self.assertEqual(self.c.player.speed, 2)
        self.c.change_speed(-10)
        self.assertEqual(self.c.player.speed, 1)

    def test_speed_upper_bound_is_two_in_both_entry_points(self):
        s = self.session('change_speed')
        s.move(10)
        s.release()
        self.c.apply_adjustment(s)
        self.assertEqual(self.c.player.speed, 2)
        self.c.change_speed(.5)
        self.assertEqual(self.c.player.speed, 2)
        self.assertEqual(self.messages, [])

    def test_speed_eof_at_pause_barrier_keeps_auto_next_and_end_timer(self):
        for boundary in ('queued', 'inside-pause', 'end-timer', 'auto-off'):
            with self.subTest(boundary=boundary):
                self.setUp()
                s = self.session('change_speed')
                s.move(.5)
                s.release()
                if boundary == 'end-timer':
                    self.c.sleep_timer = Timer()
                    self.c.sleep_timer.poll = lambda **kw: 'stop' if kw.get('ended') else None
                if boundary == 'auto-off':
                    self.c.auto_next = lambda: False
                def eof():
                    self.c.player.position = 600
                    self.c.player.is_playing = False
                    self.c.player.callback('ended', None)
                if boundary == 'inside-pause':
                    original = self.c.player.pause
                    def pause():
                        self.c.player.pause = original
                        original()
                        eof()
                    self.c.player.pause = pause
                else:
                    eof()
                self.c.apply_adjustment(s)
                while self.events:
                    self.events.pop(0)()
                if boundary in ('end-timer', 'auto-off'):
                    self.assertEqual(self.c.track_index, 0)
                    self.assertFalse(self.c.player.is_playing)
                    self.assertTrue(self.messages)
                else:
                    self.assertEqual(self.c.track_index, 1)
                    self.assertTrue(self.c.player.is_playing)

    def test_volume_clamps_each_step_and_reverses_immediately_at_limit(self):
        s = self.session('change_volume')
        s.move(1)
        s.move(1)
        s.move(-1)
        s.release()
        play = self.c.player.play = Mock()
        pause = self.c.player.pause = Mock()
        self.c.apply_adjustment(s)
        self.assertEqual(self.c.player.volume, 99)
        play.assert_not_called()
        pause.assert_not_called()

    def test_latest_target_is_read_after_slow_pause_not_before(self):
        s = self.session('seek_relative')
        s.move(15)
        original = self.c.player.pause
        def delayed_pause():
            original()
            for _ in range(10):
                s.move(15)
            s.release()
        self.c.player.pause = delayed_pause
        self.c.apply_adjustment(s)
        self.assertEqual(self.c.player.position, 265)
        self.assertTrue(self.c.player.is_playing)
        self.assertTrue(s.done)

    def test_seek_from_pause_and_to_end_never_autostarts_or_autonext(self):
        for paused, delta, target in ((True, 15, 115), (False, 1000, 600)):
            with self.subTest(paused=paused):
                self.setUp()
                if paused:
                    self.c.player.pause()
                s = self.session('seek_relative')
                s.move(delta)
                s.release()
                self.c.apply_adjustment(s)
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.c.player.position, target)
                self.assertEqual(self.c.track_index, 0)

    def test_save_error_or_cancellation_during_save_cannot_resume(self):
        for failure in ('save', 'cancel'):
            with self.subTest(failure=failure):
                self.setUp()
                s = self.session('seek_relative')
                s.move(15)
                self.c.apply_adjustment(s)
                s.release()
                play = self.c.player.play = Mock()
                if failure == 'save':
                    self.c.save = Mock(side_effect=OSError('No space'))
                    with self.assertRaises(OSError):
                        self.c.apply_adjustment(s)
                else:
                    self.c.save = lambda: s.cancel()
                    self.c.apply_adjustment(s)
                play.assert_not_called()

    def test_timer_pause_and_track_change_invalidate_late_adjustments(self):
        for boundary in ('timer', 'track', 'toggle', 'close', 'eof'):
            with self.subTest(boundary=boundary):
                self.setUp()
                s = self.session('seek_relative')
                s.move(15)
                self.c.apply_adjustment(s)
                if boundary == 'timer':
                    timer = self.c.sleep_timer = Timer()
                    timer.due = 'stop'
                    self.assertTrue(self.c.check_timer())
                elif boundary == 'track':
                    self.c.next_track()
                elif boundary == 'toggle':
                    self.c.toggle()
                    self.assertFalse(self.c.player.is_playing)
                elif boundary == 'close':
                    self.c.close()
                else:
                    # An actual EOF boundary must not be undone by stale volume.
                    self.c._handle_event(self.c.generation, 'ended', None)
                before = self.c.player.position
                playing = self.c.player.is_playing
                s.move(90)
                s.release()
                self.c.apply_adjustment(s)
                self.assertEqual(self.c.player.position, before)
                self.assertEqual(self.c.player.is_playing, playing)
                self.assertTrue(s.cancelled)


if __name__ == '__main__':
    unittest.main()
