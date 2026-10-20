"""Persistent playback contract, isolated from the user's NVDA and library."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_controller import Library, Player
import test_plugin

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('persist_test_' + name,
        ROOT / 'addon/globalPlugins/czytelnia' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlaybackPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.preferences = Mock(volume=73, speed=1.5)
        self.module = load('controller')
        self.c = self.module.Controller(Library(), Player, lambda f: f(), lambda m: None,
                                       preferences=self.preferences)

    def test_restores_before_first_book_and_preserves_between_books_and_parts(self):
        self.assertEqual((self.c.player.volume, self.c.player.speed), (73, 1.5))
        self.c.start('abc', index=0)
        self.c.next_track()
        self.c.start('other', index=0)
        self.assertEqual((self.c.player.volume, self.c.player.speed), (73, 1.5))
        self.preferences.set_volume.assert_not_called()
        self.preferences.set_speed.assert_not_called()

    def test_single_adjustment_saves_successful_value(self):
        self.c.start('abc')
        self.c.change_volume(-1)
        self.c.change_speed(.5)
        self.preferences.set_volume.assert_called_once_with(72)
        self.preferences.set_speed.assert_called_once_with(2.0)

    def test_hold_saves_once_only_after_release(self):
        for name, step, setter, expected in (
                ('change_volume', -1, self.preferences.set_volume, 70),
                ('change_speed', .5, self.preferences.set_speed, 2.0)):
            with self.subTest(name=name):
                self.c.start('abc', index=0)
                session = self.c.new_adjustment(name)
                for _ in range(3):
                    session.move(step)
                    self.c.apply_adjustment(session)
                setter.assert_not_called()
                session.release()
                self.c.apply_adjustment(session)
                setter.assert_called_once_with(expected)

    def test_failed_backend_does_not_persist_unapplied_value(self):
        self.c.start('abc')
        self.c.player.set_volume = Mock(side_effect=OSError('backend'))
        with self.assertRaises(OSError):
            self.c.change_volume(-1)
        self.preferences.set_volume.assert_not_called()

    def test_disk_failure_is_reported_without_interrupting_playback(self):
        messages = []
        self.c.notify = messages.append
        self.preferences.set_speed.side_effect = OSError('disk full')
        self.c.start('abc')
        session = self.c.new_adjustment('change_speed')
        session.move(.5)
        session.release()
        self.c.apply_adjustment(session)
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.c.player.speed, 2.0)
        self.assertTrue(any('zapisać' in m for m in messages))

    def test_shutdown_during_disk_save_does_not_resume_audio(self):
        self.c.start('abc', index=0)
        cancelled = [False]
        self.c.cancelled = lambda: cancelled[0]
        self.preferences.set_speed.side_effect = lambda value: cancelled.__setitem__(0, True)
        session = self.c.new_adjustment('change_speed')
        session.move(.5)
        session.release()
        self.c.apply_adjustment(session)
        self.assertFalse(self.c.player.is_playing)

    def test_released_speed_is_saved_when_eof_wins_pause_barrier(self):
        self.c.start('abc', index=0)
        session = self.c.new_adjustment('change_speed')
        session.move(.5)
        session.release()
        pause = self.c.player.pause
        self.c.dispatch = lambda callback: None  # EOF delivered after the pause barrier.
        def end_during_pause():
            self.c.player.pause = pause
            pause()
            self.c.player.position = self.c.player.duration
            self.c.player.callback('ended', None)
        self.c.player.pause = end_during_pause
        self.c.apply_adjustment(session)
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.speed, 2.0)
        self.preferences.set_speed.assert_called_once_with(2.0)

    def test_real_json_roundtrip_new_controller_restores_both(self):
        work = ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            path = Path(temporary) / 'settings.json'
            path.write_text('{"version":1,"auto_next":false,"automatic_messages":false}')
            prefs = load('preferences').Preferences(path)
            c = self.module.Controller(Library(), Player, lambda f: f(), lambda m: None,
                                       preferences=prefs)
            c.start('abc')
            c.change_volume(-23)
            c.change_speed(.5)
            c.close()
            loaded = load('preferences').Preferences(path)
            restored = self.module.Controller(Library(), Player, lambda f: f(), lambda m: None,
                                               preferences=loaded)
            restored.start('abc')
            self.assertEqual((restored.player.volume, restored.player.speed), (77, 1.5))
            self.assertFalse(loaded.auto_next)
            self.assertFalse(loaded.automatic_messages)


class PluginWiringTests(unittest.TestCase):
    def test_initialization_passes_actual_preferences_to_controller(self):
        fixture = test_plugin.PluginTests('test_single_press_waits_then_opens_library')
        fixture.setUp()
        try:
            fixture.plugin._initialize()
            call = fixture.modules['plugin_test.controller'].Controller.call_args
            self.assertIs(call.kwargs['preferences'], fixture.plugin._preferences)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
