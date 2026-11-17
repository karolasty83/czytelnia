"""Precise spoken remaining time without changing other time announcements."""
import unittest

import test_controller as fixtures
import test_book_status as book_fixtures


class TrackRemainingSecondsTests(unittest.TestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)

    def test_track_includes_seconds_after_minutes(self):
        self.c.start('abc', index=1)
        self.c.player.duration = 1000
        self.c.player.position = 56
        self.assertEqual(self.c.status(), '15 minut i 44 sekundy do końca odcinka.')


    def test_track_rounds_down_after_speed_conversion(self):
        self.c.start('abc', index=1)
        self.c.player.duration = 120.9
        self.c.player.position = 0.5
        self.c.player.speed = 2
        self.assertEqual(self.c.status(), '1 minuta do końca odcinka.')
        self.c.player.position = 1.1
        self.assertEqual(self.c.status(), '59 sekund do końca odcinka.')

    def test_track_subsecond_remainder_is_not_completion(self):
        self.c.start('abc', index=1)
        self.c.player.position = 119.9
        self.assertEqual(self.c.status(), '0 sekund do końca odcinka.')
        self.c.player.position = 120
        self.assertEqual(self.c.status(), 'Koniec odcinka.')

    def test_track_precision_preserves_paused_state_and_source_position(self):
        self.c.start('abc', index=1)
        self.c.player.duration = 3661
        self.c.player.position = 0
        self.c.player.pause()
        before = list(self.lib.saved)
        self.assertEqual(self.c.status(),
                         '1 godzina, 1 minuta i 1 sekunda do końca odcinka.')
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.c.player.position, 0)
        self.assertEqual(self.c.player.duration, 3661)
        self.assertEqual(self.lib.saved, before)


class BookRemainingSecondsTests(unittest.TestCase):
    def setUp(self):
        book_fixtures.BookStatusTests.setUp(self)

    def test_book_includes_hours_minutes_and_seconds(self):
        self.c.start(self.book['id'], index=1)
        self.durations[2] = 11640
        self.paths[2].write_bytes(b'long final track')
        snapshot = self.c.book_status_snapshot()
        results = self.c.probe_book_status(snapshot, lambda: False)
        self.c.player.position = 16
        self.assertEqual(self.c.complete_book_status(snapshot, results),
                         '3 godziny, 15 minut i 44 sekundy do końca książki.')
        self.assertEqual(self.c.book_status(),
                         '3 godziny, 15 minut i 44 sekundy do końca książki.')


    def test_book_rounds_down_once_after_adding_source_durations(self):
        self.durations[:] = [60.4, 120.4, 180.4]
        for path in self.paths:
            path.write_bytes(b'fractional duration fixture')
        self.c.start(self.book['id'], index=1)
        self.c.player.position = 30.1
        self.c.player.speed = 2
        before = self.c.player.position
        self.assertEqual(self.c.book_status(), '2 minuty i 15 sekund do końca książki.')
        self.assertEqual(self.c.player.position, before)

    def test_book_subsecond_remainder_is_not_completion(self):
        self.c.start(self.book['id'], index=2)
        self.c.player.position = 179.9
        self.assertEqual(self.c.book_status(), '0 sekund do końca książki.')
        self.c.player.position = 180
        self.assertEqual(self.c.book_status(), 'Koniec książki.')


class RemainingTimeFormatTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'remaining_time_text_test', fixtures.FILE.with_name('time_text.py'))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_precise_format_omits_zero_units_and_uses_polish_forms(self):
        cases = [(-5, '0 sekund'), (0, '0 sekund'), (0.9, '0 sekund'),
                 (1, '1 sekunda'), (2, '2 sekundy'), (5, '5 sekund'),
                 (12, '12 sekund'), (14, '14 sekund'), (21, '21 sekund'),
                 (22, '22 sekundy'), (59.9, '59 sekund'), (60.9, '1 minuta'),
                 (61.9, '1 minuta i 1 sekunda'), (62, '1 minuta i 2 sekundy'),
                 (72, '1 minuta i 12 sekund'), (82, '1 minuta i 22 sekundy'),
                 (944, '15 minut i 44 sekundy'),
                 (3599.9, '59 minut i 59 sekund'), (3600.9, '1 godzina'),
                 (3601, '1 godzina i 1 sekunda'), (3660, '1 godzina i 1 minuta'),
                 (3661, '1 godzina, 1 minuta i 1 sekunda'),
                 (11744, '3 godziny, 15 minut i 44 sekundy'),
                 (43200, '12 godzin'), (79200, '22 godziny')]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(self.module.format_time(seconds, include_seconds=True), expected)

    def test_default_format_still_omits_seconds_above_one_minute(self):
        self.assertEqual(self.module.format_time(11744), '3 godziny i 15 minut')
        self.assertEqual(self.module.format_time(3661, accusative=True),
                         '1 godzinę i 1 minutę')

    def test_precise_accusative_keeps_correct_singular_forms(self):
        self.assertEqual(self.module.format_time(3661, accusative=True, include_seconds=True),
                         '1 godzinę, 1 minutę i 1 sekundę')


if __name__ == '__main__':
    unittest.main()
