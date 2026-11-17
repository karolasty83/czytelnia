"""Real controller/timer boundary tests for the output-switch transaction."""
import unittest
from unittest.mock import Mock, patch
import test_plugin
import test_timer_auto_next


class OutputBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_plugin.PluginTests('test_single_press_waits_then_opens_library')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.p = self.fixture.plugin
        self.core = test_timer_auto_next.TimerAutoNextTests()
        self.core.setUp()
        self.c = self.core.c
        self.p._controller = self.c
        self.c.player.output_device = ''
        self.c.player.set_output_device = lambda value: setattr(self.c.player, 'output_device', value)
        self.pref = self.p._preferences
        self.play = self.c.player.play = Mock(wraps=self.c.player.play)
        self.enumeration = patch.object(self.fixture.module, 'list_output_devices', return_value=['USB'])
        self.enumeration.start()
        self.addCleanup(self.enumeration.stop)

    def switch(self, fail):
        if fail:
            with self.assertRaises(OSError):
                self.p._set_output_device(self.c, 'USB')
        else:
            self.p._set_output_device(self.c, 'USB')

    def test_deadline_during_save_never_resumes_on_success_or_failure(self):
        for fail in (False, True):
            for action in ('stop', 'shutdown'):
                with self.subTest(fail=fail, action=action):
                    self.c.start('abc', index=0)
                    self.c.arm_timer(minutes=1, action=action)
                    self.play.reset_mock()
                    self.core.powers.clear()
                    def save(value):
                        self.core.now += 61
                        if fail:
                            raise OSError('disk full')
                    self.pref.set_output_device.side_effect = save
                    self.switch(fail)
                    self.play.assert_not_called()
                    self.assertFalse(self.c.player.is_playing)
                    self.assertIsNone(self.c.timer_state())
                    self.assertEqual(self.core.powers, ['shutdown'] if action == 'shutdown' else [])
                    self.assertFalse(self.c.check_timer())

    def eof_at_pause(self):
        original = self.c.player.pause
        def pause():
            self.c.player.pause = original
            original()
            self.c.player.position = self.c.player.duration
            self.c.player.callback('ended', None)
        self.c.player.pause = pause

    def test_real_eof_at_pause_obeys_end_timer_even_when_json_save_fails(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.c.start('abc', index=0)
                self.c.player.output_device = ''
                self.c.arm_timer(action='shutdown')
                self.core.powers.clear()
                self.play.reset_mock()
                self.pref.set_output_device.side_effect = OSError('disk full') if fail else None
                self.eof_at_pause()
                self.switch(fail)
                self.core.drain()
                self.play.assert_not_called()
                self.assertEqual(self.c.track_index, 0)
                self.assertEqual(self.core.lib.saved[-1], ('abc', 0, 120))
                self.assertEqual(self.core.powers, ['shutdown'])
                self.assertIsNone(self.c.timer_state())

    def test_real_eof_preserves_next_track_transition_and_single_resume(self):
        self.core.preference = True
        self.c.transition = lambda *args: self.core.transitions.append(args)
        self.eof_at_pause()
        self.play.reset_mock()
        self.p._set_output_device(self.c, 'USB')
        self.core.drain()
        self.assertEqual(self.c.track_index, 1)
        self.play.assert_not_called()
        self.assertEqual(len(self.core.transitions), 1)
        _, valid, resume = self.core.transitions[0]
        self.assertTrue(valid())
        resume()
        resume()
        self.play.assert_called_once()
        self.assertEqual(self.c.player.output_device, 'USB')

    def test_output_change_inside_transition_does_not_shorten_wait(self):
        self.core.preference = True
        self.c.transition = lambda *args: self.core.transitions.append(args)
        self.core.natural_end()
        self.play.reset_mock()
        before = self.c._pending_transition
        self.p._set_output_device(self.c, 'USB')
        self.assertEqual(self.c._pending_transition, before)
        self.play.assert_not_called()
        _, valid, resume = self.core.transitions[0]
        self.assertTrue(valid())
        resume()
        resume()
        self.play.assert_called_once()


if __name__ == '__main__':
    unittest.main()
