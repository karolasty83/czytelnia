"""Pure timer tests with a manually advanced monotonic clock."""
import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/sleep_timer.py'


class SleepTimerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), 'sleep_timer.py must exist')
        spec = importlib.util.spec_from_file_location('sleep_timer_test', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.now = 100.0
        self.timer = module.SleepTimer(clock=lambda: self.now)

    def test_tracks_count_is_bound_sequential_and_duplicate_safe(self):
        self.assertIn('tracks', __import__('inspect').signature(self.timer.arm).parameters)
        self.timer.arm(tracks=3, target=('book', 2), action='shutdown')
        state = self.timer.snapshot()
        self.assertEqual((state['mode'], state['remaining_tracks'], state['target']),
                         ('tracks', 3, ('book', 2)))
        for target in (('book', 3), ('other', 2), ('book', True)):
            self.assertIsNone(self.timer.poll(target=target, ended=True))
        self.assertIsNone(self.timer.poll(target=('book', 2)))
        self.assertEqual(self.timer.snapshot()['remaining_tracks'], 3)
        self.assertIsNone(self.timer.poll(target=('book', 2), ended=True))
        self.assertIsNone(self.timer.poll(target=('book', 2), ended=True))
        self.assertEqual(self.timer.snapshot()['remaining_tracks'], 2)
        self.assertIsNone(self.timer.poll(target=('book', 3), ended=True))
        self.assertEqual(self.timer.poll(target=('book', 4), ended=True), 'shutdown')
        self.assertIsNone(self.timer.poll(target=('book', 4), ended=True))
        self.assertIsNone(self.timer.snapshot())

    def test_invalid_tracks_preserve_active_timer(self):
        self.timer.arm(minutes=1)
        before = self.timer.snapshot()
        invalid = [dict(tracks=value, target=('book', 0)) for value in
                   (True, False, 0, -1, 1.0, '2', [], float('nan'))]
        invalid += [dict(tracks=2, minutes=1, target=('book', 0)), dict(tracks=1)]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.timer.arm(**kwargs)
                self.assertEqual(self.timer.snapshot(), before)

    def test_minutes_expire_once_using_elapsed_wall_time(self):
        self.assertIsNone(self.timer.snapshot())
        self.assertIsNone(self.timer.poll(ended=True))
        self.assertFalse(self.timer.cancel())
        self.assertIsNone(self.timer.arm(minutes=2, action='sleep'))
        self.assertEqual(self.timer.snapshot(), {
            'mode': 'minutes', 'action': 'sleep', 'remaining': 120.0,
            'target': None, 'minutes': 2,
        })
        self.now += 119.5
        self.assertIsNone(self.timer.poll(ended=True))
        self.assertEqual(self.timer.snapshot()['remaining'], 0.5)
        # No playback state or pause events are needed to advance wall time.
        self.now += 0.5
        self.assertEqual(self.timer.poll(), 'sleep')
        self.assertIsNone(self.timer.snapshot())
        self.assertIsNone(self.timer.poll())
        self.assertFalse(self.timer.cancel())

    def test_end_timer_matches_bound_book_and_track_only(self):
        target = ('book-1', 3)
        self.timer.arm(target=target, action='shutdown')
        expected = {'mode': 'end', 'action': 'shutdown', 'remaining': None,
                    'target': target, 'minutes': None}
        self.assertEqual(self.timer.snapshot(), expected)
        self.now += 1000000
        self.assertIsNone(self.timer.poll(target=target))
        for other in (None, ('book-2', 3), ('book-1', 4), ['book-1', 3]):
            self.assertIsNone(self.timer.poll(target=other, ended=True))
            self.assertEqual(self.timer.snapshot(), expected)
        self.assertEqual(self.timer.poll(target=target, ended=True), 'shutdown')
        self.assertIsNone(self.timer.snapshot())
        self.assertIsNone(self.timer.poll(target=target, ended=True))

    def test_rearm_cancel_and_snapshot_copy(self):
        self.timer.arm(minutes=1)
        self.timer.snapshot()['action'] = 'shutdown'
        self.now += 70
        self.assertEqual(self.timer.snapshot()['remaining'], 0)
        self.assertEqual(self.timer.poll(), 'stop')
        self.timer.arm(target=('book', 0))
        self.timer.arm(minutes=1440, target=('book', 0))
        self.assertEqual(self.timer.snapshot()['remaining'], 86400)
        self.assertEqual(self.timer.snapshot()['mode'], 'minutes')
        self.assertTrue(self.timer.cancel())
        self.assertFalse(self.timer.cancel())
        self.now += 100000
        self.assertIsNone(self.timer.poll(ended=True, target=('book', 0)))

    def test_invalid_arm_is_rejected_without_replacing_active_timer(self):
        self.timer.arm(minutes=1)
        before = self.timer.snapshot()
        invalid = [dict(minutes=value) for value in
                   (True, False, 0, -1, 1441, 1.0, 1.5, '1', '', float('nan'),
                    float('inf'), -float('inf'), [], {}, object())]
        invalid += [dict(minutes=1, action=value) for value in
                    (None, True, '', 'hibernate', 'Sleep', 'shutdown /f', [], {})]
        invalid += [dict(target=value) for value in
                    (None, [], ['book', 0], (), ('book',), ('book', 0, 1),
                     ('', 0), (None, 0), ('book', True), ('book', -1),
                     ('book', 1.0), ('book', '0'))]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.timer.arm(**kwargs)
                self.assertEqual(self.timer.snapshot(), before)

    def test_poll_rejects_lookalike_track_identifiers(self):
        self.timer.arm(target=('book', 1))
        for target in (('book', True), ('book', 1.0)):
            self.assertIsNone(self.timer.poll(target=target, ended=True))
        self.assertEqual(self.timer.poll(target=('book', 1), ended=True), 'stop')


if __name__ == '__main__':
    unittest.main()
