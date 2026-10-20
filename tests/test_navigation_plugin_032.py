"""End-to-end worker navigation intent regressions, without GUI/audio."""
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import Mock
import test_adjustment_plugin
import test_controller

class NavigationPlugin032Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_adjustment_plugin.AdjustmentPluginTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.p = self.fixture.p
        self.c = self.fixture.core.c
        self.jobs = self.fixture.jobs

    def drain(self):
        while self.jobs:
            self.jobs.pop(0)()

    def test_navigation_while_paused_never_calls_play(self):
        self.c.player.pause()
        self.c.player.play = Mock(wraps=self.c.player.play)
        self.p.action('next_track')
        self.drain()
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.position, 0)
        self.assertFalse(self.c.player.is_playing)
        self.p.action('previous_track')
        self.drain()
        self.assertEqual(self.c.track_index, 0)
        self.c.player.play.assert_not_called()

    def test_logical_playback_during_speed_hold_continues_only_on_destination(self):
        self.assertTrue(self.p.adjust('change_speed', .5))
        self.drain()
        self.assertFalse(self.c.player.is_playing)
        self.assertTrue(self.p.is_playing())
        plays = []
        play = self.c.player.play
        self.c.player.play = lambda: (plays.append(self.c.track_index), play())
        self.p.action('next_track')
        self.drain()
        self.assertEqual(plays, [1])
        self.assertTrue(self.c.player.is_playing)

    def test_stale_cancelled_volume_hold_after_eof_does_not_pause_new_track(self):
        self.c.player.volume = 50
        self.assertTrue(self.p.adjust('change_volume', 1))
        self.drain()
        session = self.p._adjust_sessions['change_volume']
        self.c.player.position = self.c.player.duration
        self.c.player.callback('ended', None)
        while self.fixture.core.events:
            self.fixture.core.events.pop(0)()
        self.assertTrue(session.cancelled)
        self.assertEqual(self.c.track_index, 1)
        self.assertTrue(self.c.player.is_playing)
        self.p.action('next_track')
        self.drain()
        self.assertEqual(self.c.track_index, 2)
        self.assertTrue(self.c.player.is_playing)

    def test_due_stop_timer_during_hold_cannot_be_undone_by_navigation(self):
        test_controller.ControllerTests.real_timer(self.fixture.core)
        self.c.arm_timer(minutes=1, action='stop')
        self.assertTrue(self.p.adjust('change_speed', .5))
        self.drain()
        self.fixture.core.now += 61
        plays = []
        play = self.c.player.play
        self.c.player.play = lambda: (plays.append(self.c.track_index), play())
        self.p.action('next_track')
        self.drain()
        self.assertEqual(self.c.track_index, 1)
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(plays, [])

class NumberingPayloadTests(unittest.TestCase):
    def test_numbering_formatter_is_shipped(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('numbering_build032', root / 'build.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn('globalPlugins/czytelnia/numbering.py', [name for _, name in module.payload(root)])

if __name__ == '__main__':
    unittest.main()
