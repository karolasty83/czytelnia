"""Live scrub contract: continuous process, silent controls and boundaries."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
from test_controller import Library, Player, Timer


class LivePlayer(Player):
    supports_live_seek = True

    def seek(self, position, *, before_seek=None):
        # Model the optional live backend's callback/operation barrier.
        if before_seek is not None:
            before_seek()
        self.position = min(self.duration, max(0, position))
        if self.position >= self.duration:
            self.is_playing = False


class LiveSeekControllerTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/controller.py'
        spec = importlib.util.spec_from_file_location('live_seek_controller', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.lib, self.events, self.messages = Library(), [], []
        self.c = mod.Controller(self.lib, LivePlayer, self.events.append, self.messages.append)
        self.c.start('abc', 0)
        self.p = self.c.player
        self.p.position = 30
        self.messages.clear()
        self.p.pause = Mock(wraps=self.p.pause)
        self.p.play = Mock(wraps=self.p.play)
        self.p.seek = Mock(wraps=self.p.seek)

    def test_held_seek_keeps_audio_and_release_does_not_repeat_target(self):
        s = self.c.new_adjustment('seek_relative')
        saves = len(self.lib.saved)
        for _ in range(4):
            s.move(5)
            self.c.apply_adjustment(s)
            self.assertTrue(self.p.is_playing)
        self.p.pause.assert_not_called()
        self.p.play.assert_not_called()
        self.assertEqual(self.p.seek.call_count, 4)
        self.assertEqual(len(self.lib.saved), saves)
        # Real audio has advanced since the last scrub target.
        self.p.position = 50.2
        s.release()
        self.c.apply_adjustment(s)
        self.assertEqual(self.p.seek.call_count, 4)
        self.assertTrue(s.done)
        self.assertEqual(self.lib.saved[-1], ('abc', 0, 50.2))
        self.assertEqual(self.messages, [])

    def test_single_step_is_live_and_does_not_pause(self):
        self.c.seek_relative(5)
        self.assertTrue(self.p.is_playing)
        self.assertEqual(self.p.position, 35)
        self.p.pause.assert_not_called()
        self.p.play.assert_not_called()

    def test_paused_seek_stays_paused_and_end_seek_does_not_advance(self):
        for paused, delta, target in ((True, 5, 35), (False, 500, 120)):
            with self.subTest(paused=paused):
                self.setUp()
                if paused:
                    self.p.is_playing = False
                s = self.c.new_adjustment('seek_relative')
                s.move(delta)
                self.c.apply_adjustment(s)
                s.release()
                self.c.apply_adjustment(s)
                self.assertFalse(self.p.is_playing)
                self.assertEqual(self.p.position, target)
                self.assertEqual(self.c.track_index, 0)

    def test_expired_timer_stops_audio_and_cancels_held_target(self):
        s = self.c.new_adjustment('seek_relative')
        s.move(5)
        self.c.apply_adjustment(s)
        timer = self.c.sleep_timer = Timer()
        timer.due = 'stop'
        s.move(5)
        self.c.apply_adjustment(s)
        self.assertTrue(s.cancelled)
        self.assertFalse(self.p.is_playing)
        self.assertEqual(self.p.seek.call_count, 1)

    def test_reverse_from_end_resumes_preview_but_not_from_original_pause(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                self.setUp()
                self.p.is_playing = not paused
                s = self.c.new_adjustment('seek_relative')
                s.move(500)
                self.c.apply_adjustment(s)
                self.assertFalse(self.p.is_playing)
                s.move(-5)
                self.c.apply_adjustment(s)
                self.assertEqual(self.p.is_playing, not paused)
                self.assertEqual(self.p.position, 115)
                s.release()
                self.c.apply_adjustment(s)
                self.assertEqual(self.p.is_playing, not paused)
                self.assertEqual(self.p.play.call_count, 0 if paused else 1)

    def test_natural_end_during_hold_stops_without_auto_next(self):
        s = self.c.new_adjustment('seek_relative')
        s.move(5)
        self.c.apply_adjustment(s)
        self.p.position = self.p.duration
        self.p.is_playing = False
        self.p.callback('ended', None)
        while self.events:
            self.events.pop(0)()
        self.assertEqual(self.c.track_index, 0)
        self.assertFalse(self.p.is_playing)
        self.assertTrue(s.cancelled)

    def test_eof_inside_release_save_retains_preview_classification(self):
        s = self.c.new_adjustment('seek_relative')
        s.move(5)
        self.c.apply_adjustment(s)
        s.release()
        original_save = self.c.save

        def finish_during_save():
            self.c.save = original_save
            self.p.position = self.p.duration
            self.p.is_playing = False
            self.p.callback('ended', None)
            self.p.callback('ended', None)
            original_save()

        self.c.save = finish_during_save
        self.c.apply_adjustment(s)
        self.assertTrue(s.done)
        while self.events:
            self.events.pop(0)()
        self.assertEqual(self.c.track_index, 0)
        self.assertFalse(self.p.is_playing)
        self.assertEqual(len(self.p.loaded), 1)
        self.p.play.assert_not_called()

    def test_failed_preview_invalidates_queued_work_even_if_pause_raises(self):
        for stage in ('seek', 'save'):
            for pause_raises in (False, True):
                with self.subTest(stage=stage, pause_raises=pause_raises):
                    self.setUp()
                    s = self.c.new_adjustment('seek_relative')
                    s.move(5)
                    if stage == 'save':
                        self.c.apply_adjustment(s)
                        s.release()
                    queued = self.c.new_adjustment('change_speed')
                    queued.move(.5)
                    queued.release()
                    context = self.c.adjustment_context
                    error = OSError('original live seek failure')
                    original_save = self.c.save

                    def fail(*args, **kwargs):
                        self.p.callback('ended', None)
                        raise error

                    def pause_barrier():
                        self.p.is_playing = False
                        self.p.callback('ended', None)
                        if pause_raises:
                            raise RuntimeError('pause failed too')

                    if stage == 'seek':
                        self.p.seek.side_effect = fail
                    else:
                        self.c.save = fail
                    self.p.pause.side_effect = pause_barrier
                    with self.assertRaises(OSError) as caught:
                        self.c.apply_adjustment(s)
                    self.assertIs(caught.exception, error)
                    self.assertNotEqual(self.c.adjustment_context, context)
                    self.assertTrue(s.cancelled)
                    self.assertIsNone(self.c._adjustment)
                    self.p.pause.side_effect = None
                    self.p.seek.side_effect = None
                    self.c.save = original_save
                    self.c.apply_adjustment(queued)
                    self.assertTrue(queued.cancelled)
                    while self.events:
                        self.events.pop(0)()
                    self.assertEqual(self.c.track_index, 0)
                    self.assertFalse(self.p.is_playing)
                    self.assertEqual(len(self.p.loaded), 1)
                    self.p.play.assert_not_called()

    def test_direct_seek_failure_stops_audio_and_invalidates_callbacks(self):
        for stage in ('seek', 'save'):
            for pause_raises in (False, True):
                with self.subTest(stage=stage, pause_raises=pause_raises):
                    self.setUp()
                    error = OSError('direct seek failure')
                    original_save = self.c.save
                    captured = []

                    def fail(*args, **kwargs):
                        # A UI gesture can be captured during blocking I/O.
                        captured.append(self.c.new_adjustment('change_speed'))
                        self.p.callback('ended', None)
                        raise error

                    def pause_barrier():
                        self.p.is_playing = False
                        self.p.callback('ended', None)
                        if pause_raises:
                            raise RuntimeError('pause failed too')

                    if stage == 'seek':
                        self.p.seek.side_effect = fail
                    else:
                        self.c.save = fail
                    self.p.pause.side_effect = pause_barrier
                    with self.assertRaises(OSError) as caught:
                        self.c.seek_relative(5)
                    self.assertIs(caught.exception, error)
                    self.assertFalse(self.p.is_playing)
                    self.p.pause.assert_called_once()
                    self.p.pause.side_effect = None
                    self.p.seek.side_effect = None
                    self.c.save = original_save
                    queued, = captured
                    queued.move(.5)
                    queued.release()
                    self.c.apply_adjustment(queued)
                    self.assertTrue(queued.cancelled)
                    while self.events:
                        self.events.pop(0)()
                    self.assertEqual(self.c.track_index, 0)
                    self.assertFalse(self.p.is_playing)
                    self.assertEqual(len(self.p.loaded), 1)
                    self.p.play.assert_not_called()

    def test_gesture_captured_during_failure_pause_cannot_restart_audio(self):
        for held in (False, True):
            for stage in ('seek', 'save'):
                for pause_raises in (False, True):
                    with self.subTest(held=held, stage=stage, pause_raises=pause_raises):
                        self.setUp()
                        s = self.c.new_adjustment('seek_relative') if held else None
                        if s:
                            s.move(5)
                            if stage == 'save':
                                self.c.apply_adjustment(s)
                                s.release()
                        original_save = self.c.save
                        error = OSError('original failure')
                        captured = []

                        def pause_barrier():
                            # The UI runs while pause is draining the backend,
                            # after cleanup's first adjustment invalidation.
                            captured.append(self.c.new_adjustment('change_speed'))
                            self.p.is_playing = False
                            self.p.callback('ended', None)
                            if pause_raises:
                                raise RuntimeError('cleanup failure')

                        if stage == 'seek':
                            self.p.seek.side_effect = error
                        else:
                            self.c.save = Mock(side_effect=error)
                        self.p.pause.side_effect = pause_barrier
                        with self.assertRaises(OSError) as caught:
                            if held:
                                self.c.apply_adjustment(s)
                            else:
                                self.c.seek_relative(5)
                        self.assertIs(caught.exception, error)
                        self.p.pause.side_effect = None
                        self.p.seek.side_effect = None
                        self.c.save = original_save
                        queued, = captured
                        self.assertIsNotNone(queued)
                        queued.move(.5)
                        queued.release()
                        self.c.apply_adjustment(queued)
                        self.assertTrue(queued.cancelled)
                        while self.events:
                            self.events.pop(0)()
                        self.assertFalse(self.p.is_playing)
                        self.p.play.assert_not_called()
                        self.assertEqual(self.p.speed, 1.0)
                        self.assertEqual(self.c.track_index, 0)
                        self.assertEqual(len(self.p.loaded), 1)

    def test_timer_expiring_inside_reversal_seek_prevents_resume(self):
        s = self.c.new_adjustment('seek_relative')
        s.move(500)
        self.c.apply_adjustment(s)
        timer = self.c.sleep_timer = Timer()

        def seek_after_deadline(position, *, before_seek=None):
            LivePlayer.seek(self.p, position, before_seek=before_seek)
            timer.due = 'stop'

        self.p.seek.side_effect = seek_after_deadline
        s.move(-5)
        self.c.apply_adjustment(s)
        self.p.play.assert_not_called()
        self.assertFalse(self.p.is_playing)
        self.assertTrue(s.cancelled)
        self.assertEqual(self.lib.saved[-1], ('abc', 0, 115))

    def real_timer(self):
        path = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/sleep_timer.py'
        spec = importlib.util.spec_from_file_location('live_seek_timer', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        timer = self.c.sleep_timer = mod.SleepTimer(clock=lambda: 100.0)
        timer.poll = Mock(wraps=timer.poll)
        return timer

    def test_live_seek_handoff_keeps_new_eof_and_invalidates_old_eof(self):
        for held in (False, True):
            for tracks in (None, 1, 2):
                for new_eof in (False, True):
                    with self.subTest(held=held, tracks=tracks, new_eof=new_eof):
                        self.setUp()
                        timer = self.real_timer()
                        powers = []
                        self.c.power = powers.append
                        self.c.arm_timer(tracks=tracks, action='sleep')
                        old_generation = self.c.generation

                        def seek_with_racing_eof(position, *, before_seek=None):
                            # Drained old EOF precedes the backend handoff.
                            self.assertEqual(self.c.generation, old_generation)
                            self.assertFalse(self.c._end_lock.locked())
                            self.p.callback('ended', None)
                            LivePlayer.seek(self.p, position, before_seek=before_seek)
                            if new_eof:
                                # The new-position seek has completed. A callback
                                # can run before the controller resumes from seek.
                                self.p.position = self.p.duration
                                self.p.is_playing = False
                                self.p.callback('ended', None)
                                self.p.callback('ended', None)

                        self.p.seek.side_effect = seek_with_racing_eof
                        if held:
                            s = self.c.new_adjustment('seek_relative')
                            s.move(5)
                            self.c.apply_adjustment(s)
                        else:
                            self.c.seek_relative(5)
                        while self.events:
                            self.events.pop(0)()
                        end_polls = [call for call in timer.poll.call_args_list
                                     if call.kwargs.get('ended')]
                        self.assertEqual(len(end_polls), int(new_eof))
                        if not new_eof:
                            self.assertEqual(self.c.track_index, 0)
                            self.assertTrue(self.p.is_playing)
                            self.assertEqual(self.p.position, 35)
                            self.assertIsNotNone(timer.snapshot())
                            if tracks is not None:
                                self.assertEqual(timer.snapshot()['remaining_tracks'], tracks)
                            self.assertEqual(powers, [])
                        elif tracks == 2:
                            self.assertEqual(timer.snapshot()['remaining_tracks'], 1)
                            self.assertEqual(self.c.track_index, 0 if held else 1)
                            self.assertEqual(self.p.is_playing, not held)
                            self.assertEqual(powers, [])
                        else:
                            self.assertIsNone(timer.snapshot())
                            self.assertEqual(powers, ['sleep'])
                            self.assertEqual(self.c.track_index, 0)
                            self.assertFalse(self.p.is_playing)
                        if held:
                            self.p.play.assert_not_called()

    def test_release_save_eof_counts_once_and_end_timer_still_fires(self):
        for tracks in (None, 1, 2):
            with self.subTest(tracks=tracks):
                self.setUp()
                timer = self.real_timer()
                powers = []
                self.c.power = powers.append
                self.c.arm_timer(tracks=tracks, action='sleep')
                s = self.c.new_adjustment('seek_relative')
                s.move(5)
                self.c.apply_adjustment(s)
                s.release()
                original_save = self.c.save

                def end_in_save():
                    self.c.save = original_save
                    self.p.position = self.p.duration
                    self.p.is_playing = False
                    self.p.callback('ended', None)
                    self.p.callback('ended', None)
                    original_save()

                self.c.save = end_in_save
                self.c.apply_adjustment(s)
                while self.events:
                    self.events.pop(0)()
                self.assertEqual(self.c.track_index, 0)
                self.assertFalse(self.p.is_playing)
                end_polls = [call for call in timer.poll.call_args_list
                             if call.kwargs.get('ended')]
                self.assertEqual(len(end_polls), 1)
                if tracks == 2:
                    self.assertEqual(timer.snapshot()['remaining_tracks'], 1)
                    self.assertEqual(powers, [])
                    # Manual continuation must not count the preview end twice.
                    self.c.start('abc')
                    self.assertEqual(self.c.track_index, 1)
                    self.p.position = self.p.duration
                    self.p.is_playing = False
                    self.p.callback('ended', None)
                    self.p.callback('ended', None)
                    while self.events:
                        self.events.pop(0)()
                    self.assertEqual(self.c.track_index, 1)
                self.assertIsNone(timer.snapshot())
                self.assertEqual(powers, ['sleep'])
                self.assertFalse(self.p.is_playing)

    def test_genuine_post_release_eof_is_not_suppressed(self):
        for stale_preview in (False, True):
            with self.subTest(stale_preview=stale_preview):
                self.setUp()
                timer = self.real_timer()
                self.c.arm_timer(tracks=2)
                s = self.c.new_adjustment('seek_relative')
                s.move(5)
                self.c.apply_adjustment(s)
                if stale_preview:
                    self.p.position = self.p.duration
                    self.p.is_playing = False
                    self.p.callback('ended', None)
                    s.move(-5)
                    self.c.apply_adjustment(s)
                s.release()
                self.c.apply_adjustment(s)
                self.p.position = self.p.duration
                self.p.is_playing = False
                self.p.callback('ended', None)
                self.p.callback('ended', None)
                while self.events:
                    self.events.pop(0)()
                self.assertEqual(self.c.track_index, 1)
                self.assertTrue(self.p.is_playing)
                self.assertEqual(len(self.p.loaded), 2)
                self.assertEqual(timer.snapshot()['remaining_tracks'], 1)
                end_polls = [call for call in timer.poll.call_args_list
                             if call.kwargs.get('ended')]
                self.assertEqual(len(end_polls), 1)

    def test_save_failure_pauses_live_audio(self):
        s = self.c.new_adjustment('seek_relative')
        s.move(5)
        self.c.apply_adjustment(s)
        s.release()
        self.c.save = Mock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError):
            self.c.apply_adjustment(s)
        self.assertFalse(self.p.is_playing)
        self.assertTrue(s.cancelled)

    def test_seek_failure_stops_audio_and_cancels_session(self):
        s = self.c.new_adjustment('seek_relative')
        s.move(5)
        self.p.seek.side_effect = OSError('native seek failed')
        with self.assertRaises(OSError):
            self.c.apply_adjustment(s)
        self.assertTrue(s.cancelled)
        self.assertFalse(self.p.is_playing)


if __name__ == '__main__':
    unittest.main()
