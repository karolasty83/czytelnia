"""Manual resume uses saved end positions, not a new library schema."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import Mock

import test_controller as fixtures
from test_persistent_durations import library, controller, WORK


class RewindingPlayer(fixtures.Player):
    """Mirror the real backend's rewind-on-play so regressions are visible."""
    def __init__(self, callback):
        super().__init__(callback)
        self.plays = []

    def play(self):
        self.plays.append((self.loaded[-1][0], self.position))
        if self.position >= self.duration:
            self.position = 0
        super().play()


class CompletedResumeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        source = self.root / 'source'
        source.mkdir()
        for index in range(3):
            (source / ('%d.wav' % index)).write_bytes(b'synthetic audio fixture')
        self.probe = Mock(side_effect=lambda path: (60, 120, 180)[int(path.stem)])
        self.lib = library.Library(self.root / 'config', duration_probe=self.probe)
        self.book = self.lib.import_book(source)
        self.key = self.book['id']
        self.pending, self.messages, self.transitions = [], [], []
        self.new_controller()

    def new_controller(self):
        self.c = controller.Controller(self.lib, RewindingPlayer, self.pending.append,
            self.messages.append, auto_next=lambda: False,
            transition=lambda *args: self.transitions.append(args))

    def drain(self):
        while self.pending:
            self.pending.pop(0)()

    def complete(self, index=0):
        self.c.start(self.key, index=index)
        self.c.player.position = self.c.player.duration
        self.c.player.is_playing = False
        self.c.player.callback('ended', None)
        self.drain()

    def test_normal_start_after_real_library_restart_skips_saved_completed_part(self):
        self.complete()
        self.assertEqual(len(self.c.player.loaded), 1)
        self.assertEqual(self.c.track_index, 0)
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.transitions, [])
        self.c.close()
        no_probe = Mock(side_effect=AssertionError('unchanged media must not be probed'))
        self.lib = library.Library(self.root / 'config', duration_probe=no_probe)
        saved = self.lib.get_book(self.key)
        self.assertEqual((saved['track_index'], saved['position']), (0, 60))
        self.new_controller()
        self.c.start(self.key)
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.plays, [(self.lib.track_path(self.key, 1), 0)])
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.transitions, [])
        no_probe.assert_not_called()

    def test_toggle_after_end_advances_once_preserving_count_timer(self):
        fixtures.ControllerTests.real_timer(self)
        self.c.start(self.key, index=0)
        self.c.arm_timer(tracks=2, action='shutdown')
        self.c.player.position = 60
        self.c.player.is_playing = False
        self.c.player.callback('ended', None)
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
        self.assertEqual(self.powers, [])
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.loaded, [(self.lib.track_path(self.key, 0), 0),
                                              (self.lib.track_path(self.key, 1), 0)])
        # The count timer now advances automatically; pause the pending start,
        # then explicitly resume the already prepared next part.
        self.c.toggle(selected_book_id='ignored-selection')
        self.assertFalse(self.c.player.is_playing)
        self.c.toggle(selected_book_id='ignored-selection')
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.position, 0)
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
        self.drain()  # old queued EOF cannot skip or count the newly started part
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
        self.c.player.position = 120
        self.c.player.is_playing = False
        self.c.player.callback('ended', None)
        self.c.player.callback('ended', None)
        self.drain()
        self.assertEqual(self.powers, ['shutdown'])
        self.assertIsNone(self.c.timer_state())
        self.assertEqual(self.c.track_index, 1)
        self.assertFalse(self.c.player.is_playing)
        saved = self.lib.get_book(self.key)
        self.assertEqual((saved['track_index'], saved['position']), (1, 120))

    def test_fifo_resume_ahead_of_unprocessed_eof_counts_both_parts_once(self):
        for command in ('toggle', 'start'):
            with self.subTest(command=command):
                self.new_controller()
                fixtures.ControllerTests.real_timer(self)
                self.c.start(self.key, index=0)
                self.c.arm_timer(tracks=2, action='shutdown')
                entered, release = threading.Event(), threading.Event()
                callbacks = []
                with ThreadPoolExecutor(max_workers=1) as worker:
                    self.c.dispatch = lambda callback: callbacks.append(worker.submit(callback))

                    def block_worker():
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('worker not released')

                    blocker = worker.submit(block_worker)
                    try:
                        self.assertTrue(entered.wait(5))
                        resume = worker.submit(self.c.toggle if command == 'toggle'
                                               else lambda: self.c.start(self.key))
                        # The user's command is FIFO-ahead of the actual player EOF.
                        self.c.player.position = 60
                        self.c.player.is_playing = False
                        self.c.player.callback('ended', None)
                        self.c.player.callback('ended', None)
                        self.assertEqual(len(callbacks), 2)
                    finally:
                        release.set()
                    blocker.result(timeout=5)
                    resume.result(timeout=5)
                    for callback in callbacks:
                        callback.result(timeout=5)
                    self.assertEqual(self.c.track_index, 1)
                    self.assertTrue(self.c.player.is_playing)
                    self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
                    self.assertEqual(self.powers, [])
                    self.assertEqual(self.transitions, [])
                    self.c.player.position = 120
                    self.c.player.is_playing = False
                    self.c.player.callback('ended', None)
                    self.c.player.callback('ended', None)
                    for callback in callbacks:
                        callback.result(timeout=5)
                self.assertEqual(self.powers, ['shutdown'])
                self.assertIsNone(self.c.timer_state())
                self.assertEqual(self.c.track_index, 1)
                self.assertFalse(self.c.player.is_playing)
                saved = self.lib.get_book(self.key)
                self.assertEqual((saved['track_index'], saved['position']), (1, 120))
                self.c.close()

    def test_pending_due_end_timer_stops_before_manual_resume(self):
        for command in ('toggle', 'start'):
            for mode in ('end', 'tracks'):
                for action in ('stop', 'sleep', 'shutdown'):
                    with self.subTest(command=command, mode=mode, action=action):
                        self.new_controller()
                        fixtures.ControllerTests.real_timer(self)
                        self.c.start(self.key, index=0)
                        self.c.arm_timer(action=action, **({'tracks': 1} if mode == 'tracks' else {}))
                        self.messages.clear()
                        self.c.player.position = 60
                        self.c.player.is_playing = False
                        self.c.player.callback('ended', None)
                        self.c.player.callback('ended', None)
                        before = list(self.c.player.plays)
                        if command == 'toggle':
                            self.c.toggle()
                        else:
                            self.c.start(self.key)
                        self.drain()
                        self.assertIsNone(self.c.timer_state())
                        self.assertEqual(self.c.track_index, 0)
                        self.assertFalse(self.c.player.is_playing)
                        self.assertEqual(self.c.player.plays, before)
                        self.assertEqual(len(self.c.player.loaded), 1)
                        self.assertEqual(self.powers, [] if action == 'stop' else [action])
                        self.assertEqual(self.messages,
                            ['Wyłącznik czasowy: zatrzymano książkę.'] if action == 'stop' else [])
                        saved = self.lib.get_book(self.key)
                        self.assertEqual((saved['track_index'], saved['position']), (0, 60))
                        self.c.close()

    def test_normal_start_claims_eof_delivered_by_player_inside_pause_barrier(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.new_controller()
                fixtures.ControllerTests.real_timer(self)
                self.c.start(self.key, index=0)
                self.c.arm_timer(tracks=count, action='sleep')
                self.c.player.position = 59
                original_pause = self.c.player.pause

                def pause_with_callback_barrier():
                    self.c.player.pause = original_pause
                    # A second thread emits EOF while the serialized command is
                    # in pause; joining models the backend's callback barrier.
                    def finish():
                        self.c.player.position = 60
                        self.c.player.is_playing = False
                        self.c.player.callback('ended', None)
                        self.c.player.callback('ended', None)
                    with ThreadPoolExecutor(max_workers=1) as player_thread:
                        player_thread.submit(finish).result(timeout=5)
                    original_pause()

                self.c.player.pause = pause_with_callback_barrier
                self.c.start(self.key)
                self.assertEqual(len(self.pending), 2, 'EOF must arrive inside pause')
                self.drain()
                if count == 1:
                    self.assertEqual(self.powers, ['sleep'])
                    self.assertIsNone(self.c.timer_state())
                    self.assertEqual(self.c.track_index, 0)
                    self.assertFalse(self.c.player.is_playing)
                    self.assertEqual(len(self.c.player.plays), 1)
                else:
                    self.assertEqual(self.powers, [])
                    self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
                    self.assertEqual(self.c.track_index, 1)
                    self.assertTrue(self.c.player.is_playing)
                    self.assertEqual(len(self.c.player.plays), 2)
                self.c.close()

    def test_seek_to_duration_does_not_fabricate_a_natural_end_on_resume(self):
        for command in ('toggle', 'start'):
            with self.subTest(command=command):
                self.new_controller()
                fixtures.ControllerTests.real_timer(self)
                self.c.start(self.key, index=0)
                self.c.arm_timer(tracks=2, action='shutdown')
                self.c.seek_relative(60)
                self.assertEqual(self.pending, [])
                if command == 'toggle':
                    self.c.toggle()
                else:
                    self.c.start(self.key)
                self.assertEqual(self.c.track_index, 1)
                self.assertTrue(self.c.player.is_playing)
                self.assertEqual(self.c.timer_state()['remaining_tracks'], 2)
                self.assertEqual(self.powers, [])
                self.c.close()

    def test_final_completed_book_stays_ended_until_explicit_replay(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.complete(index=2)
                plays = list(self.c.player.plays)
                self.c.toggle()
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.c.player.position, 180)
                self.assertEqual(self.c.player.plays, plays)
                self.c.close()
                self.lib = library.Library(self.root / 'config', duration_probe=self.probe)
                self.new_controller()
                if restart:
                    self.c.toggle(selected_book_id=self.key)
                else:
                    self.c.start(self.key)
                self.assertEqual(self.c.track_index, 2)
                self.assertEqual(self.c.player.position, 180)
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.c.player.plays, [])
                self.assertEqual(self.messages[-1], 'Koniec książki.')
                self.c.toggle()
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.c.player.plays, [])
                self.c.start(self.key, index=2)
                self.assertEqual(self.c.player.position, 0)
                self.assertTrue(self.c.player.is_playing)
                self.assertEqual(self.c.player.plays, [(self.lib.track_path(self.key, 2), 0)])

    def test_normal_start_preserves_pending_count_but_explicit_next_cancels(self):
        fixtures.ControllerTests.real_timer(self)
        self.c.start(self.key, index=0)
        self.c.arm_timer(tracks=3)
        self.c.player.position = 60
        self.c.player.is_playing = False
        self.c.player.callback('ended', None)
        self.drain()
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 2)
        self.c.start(self.key)
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 2)
        self.c.next_track()
        self.assertIsNone(self.c.timer_state())
        self.assertEqual(self.c.track_index, 2)
        self.assertEqual(self.powers, [])

    def test_cold_toggle_resumes_next_part_and_close_persists_its_position(self):
        self.lib.save_position(self.key, 0, 60)
        self.lib = library.Library(self.root / 'config', duration_probe=self.probe)
        self.new_controller()
        self.c.toggle(selected_book_id=self.key)
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.position, 0)
        self.c.player.position = 22.5
        self.c.close()
        restored = library.Library(self.root / 'config', duration_probe=self.probe)
        saved = restored.get_book(self.key)
        self.assertEqual((saved['track_index'], saved['position']), (1, 22.5))

    def test_incomplete_saved_position_does_not_skip_and_explicit_selection_replays(self):
        for position in (0, 30, 59.99):
            with self.subTest(position=position):
                self.new_controller()
                self.lib.save_position(self.key, 0, position)
                self.c.start(self.key)
                self.assertEqual(self.c.track_index, 0)
                self.assertEqual(self.c.player.position, position)
                self.assertTrue(self.c.player.is_playing)
                self.c.close()
        self.new_controller()
        self.lib.save_position(self.key, 0, 60)
        self.c.start(self.key, index=0)
        self.assertEqual(self.c.track_index, 0)
        self.assertEqual(self.c.player.position, 0)
        self.assertTrue(self.c.player.is_playing)

    def test_changed_saved_media_uses_current_duration_to_resolve_completion(self):
        self.lib.save_position(self.key, 0, 60)
        path = self.lib.track_path(self.key, 0)
        path.write_bytes(b'longer changed synthetic fixture')
        self.probe.side_effect = lambda path: 90
        self.probe.reset_mock()
        self.c.start(self.key)
        self.assertEqual(self.c.track_index, 0)
        self.assertEqual(self.c.player.position, 60)
        self.assertEqual(self.c.player.duration, 90)
        self.probe.assert_called_once_with(path)

    def test_manual_resume_failure_keeps_completed_position_and_retries_next(self):
        for stage in ('duration', 'load', 'save'):
            with self.subTest(stage=stage):
                self.complete()
                self.c.player.callback('ended', None)
                before = self.lib.get_book(self.key)
                target, name = {'duration': (self.lib, 'track_duration'),
                    'load': (self.c.player, 'load'),
                    'save': (self.lib, 'save_position')}[stage]
                original = getattr(target, name)
                setattr(target, name, Mock(side_effect=OSError('synthetic failure')))
                with self.assertRaisesRegex(OSError, 'synthetic failure'):
                    self.c.toggle()
                setattr(target, name, original)
                self.assertEqual(self.c.track_index, 0)
                self.assertEqual(self.c.player.position, 60)
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.lib.get_book(self.key), before)
                self.drain()
                self.c.toggle()
                self.assertEqual(self.c.track_index, 1)
                self.assertEqual(self.c.player.position, 0)
                self.assertTrue(self.c.player.is_playing)
                self.assertEqual(self.transitions, [])

    def test_minute_timer_survives_manual_resume_then_saves_new_part_on_expiry(self):
        fixtures.ControllerTests.real_timer(self)
        self.c.start(self.key, index=0)
        self.c.arm_timer(minutes=1, action='sleep')
        self.c.player.position = 60
        self.c.player.is_playing = False
        self.c.player.callback('ended', None)
        self.drain()
        self.c.toggle()
        self.assertEqual(self.c.timer_state()['mode'], 'minutes')
        self.assertEqual(self.c.track_index, 1)
        self.c.player.position = 20
        self.now += 60
        self.messages.clear()
        self.assertTrue(self.c.check_timer())
        self.assertEqual(self.powers, ['sleep'])
        self.assertEqual(self.messages, [])
        self.assertFalse(self.c.player.is_playing)
        saved = self.lib.get_book(self.key)
        self.assertEqual((saved['track_index'], saved['position']), (1, 20))


if __name__ == '__main__':
    unittest.main()
