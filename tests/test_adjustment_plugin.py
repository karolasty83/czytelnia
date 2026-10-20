import unittest
from unittest.mock import Mock
import test_plugin
import test_adjustments


class AdjustmentPluginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_plugin.PluginTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.core = test_adjustments.AdjustmentTests()
        self.core.setUp()
        self.p = self.fixture.plugin
        self.p._executor.shutdown(wait=True)
        self.p._executor = Mock()
        self.jobs = []
        self.p._executor.submit.side_effect = self.jobs.append
        self.p._controller = self.core.c
        self.p._say = Mock()
        self.p._deliver_message = Mock()

    def test_repeated_volume_uses_one_job_latest_target_no_busy_message(self):
        self.assertTrue(hasattr(self.p, 'adjust'), 'Brak laczenia szybkich polecen')
        self.core.c.player.volume = 50
        for _ in range(20):
            self.assertTrue(self.p.adjust('change_volume', 1))
        self.assertEqual(len(self.jobs), 1)
        self.p.end_adjustment()
        self.assertEqual(len(self.jobs), 1)
        self.jobs.pop(0)()
        self.assertEqual(self.core.c.player.volume, 70)
        self.assertEqual(self.jobs, [])
        self.p._say.assert_not_called()
        self.p._deliver_message.assert_called_once_with('Głośność 70 procent.')


    def test_normal_command_finishes_latest_seek_before_execution(self):
        self.assertTrue(hasattr(self.p, 'adjust'))
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        self.p.adjust('seek_relative', 15)
        self.p.action('toggle')
        while self.jobs:
            self.jobs.pop(0)()
        self.assertEqual(self.core.c.player.position, 130)
        self.assertFalse(self.core.c.player.is_playing)
        self.assertIsNone(self.p._adjust_session)

    def test_volume_updates_during_slow_ack_coalesce_into_one_final_apply(self):
        self.assertTrue(hasattr(self.p, 'adjust'))
        c = self.core.c
        c.player.volume = 50
        calls = []
        def slow_ack(value):
            calls.append(value)
            if len(calls) == 1:
                for _ in range(20):
                    self.assertTrue(self.p.adjust('change_volume', -1))
                self.p.end_adjustment()
                self.assertEqual(self.jobs, [])  # no job per incoming step
            c.player.volume = value
        c.player.set_volume = slow_ack
        self.p.adjust('change_volume', -1)
        self.jobs.pop(0)()
        self.assertEqual(len(self.jobs), 1)
        self.p._deliver_message.assert_not_called()  # stale value must not speak
        self.jobs.pop(0)()
        self.assertEqual(calls, [49, 29])
        self.assertEqual(c.player.volume, 29)
        self.assertEqual(self.jobs, [])
        self.p._deliver_message.assert_called_once_with('Głośność 29 procent.')
        self.p._say.assert_not_called()

    def test_terminate_cancels_queued_adjustment_and_cannot_restart_audio(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        self.p.adjust('seek_relative', 15)
        self.p.end_adjustment()
        play = self.core.c.player.play = Mock(wraps=self.core.c.player.play)
        self.p.terminate()
        while self.jobs:
            self.jobs.pop(0)()
        play.assert_not_called()
        self.assertTrue(self.core.c.closed)
        self.assertFalse(self.core.c.player.is_playing)

    def test_timer_interrupts_hold_until_release_instead_of_moving_paused_book(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        self.core.c.cancel_adjustment()  # same invalidation used by sleep timer
        self.assertFalse(self.p.adjust('seek_relative', 15))
        self.assertEqual(self.core.c.player.position, 115)
        self.p.end_adjustment()
        self.assertTrue(self.p.adjust('seek_relative', -15))  # fresh deliberate gesture

    def test_paused_seek_still_allows_switch_to_volume_and_speed(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        self.assertFalse(self.core.c.player.is_playing)
        self.assertTrue(self.p.is_playing(), 'Keyboard must see logical playback during seek')
        self.assertTrue(self.p.adjust('change_volume', -1))
        self.assertTrue(self.p.adjust('change_speed', .5))
        self.p.end_adjustment()
        while self.jobs:
            self.jobs.pop(0)()
        self.assertEqual(self.core.c.player.speed, 1.5)

    def test_mixed_targets_remain_bounded_and_keep_latest_with_delayed_ui(self):
        callbacks = []
        self.fixture.modules['wx'].CallAfter = lambda fn, *args: callbacks.append(lambda: fn(*args))
        self.core.c.player.volume = 50
        for _ in range(10):
            for name, delta in [('change_volume', 1), ('seek_relative', 15), ('change_speed', .5)]:
                self.assertTrue(self.p.adjust(name, delta))
        self.assertEqual(len(self.jobs), 1)
        self.assertLessEqual(len(self.p._adjust_sessions), 3)
        self.p.end_adjustment()
        guard = 0
        while self.jobs or callbacks:
            guard += 1
            self.assertLess(guard, 20)
            if self.jobs:
                self.jobs.pop(0)()
            if callbacks:
                callbacks.pop(0)()
            self.assertLessEqual(len(self.jobs), 1)
        self.assertEqual(self.core.c.player.volume, 60)
        self.assertEqual(self.core.c.player.position, 250)
        self.assertEqual(self.core.c.player.speed, 2)
        self.assertEqual(self.p._adjust_sessions, {})

    def test_release_after_timer_allows_first_fresh_gesture(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        self.core.c.cancel_adjustment()
        self.p.end_adjustment()
        self.assertTrue(self.p.adjust('seek_relative', -15))

    def test_switching_kinds_preserves_each_latest_accepted_target(self):
        self.core.c.player.volume = 50
        for name, delta in [('change_volume', 1), ('change_speed', .5), ('change_volume', 1)]:
            self.assertTrue(self.p.adjust(name, delta))
        self.p.end_adjustment()
        while self.jobs:
            self.jobs.pop(0)()
        self.assertEqual(self.core.c.player.volume, 52)
        self.assertEqual(self.core.c.player.speed, 1.5)

    def test_pause_during_seek_does_not_restart_before_pausing(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        self.p.adjust('seek_relative', 15)
        play = self.core.c.player.play = Mock(wraps=self.core.c.player.play)
        self.p.action('toggle')
        while self.jobs:
            self.jobs.pop(0)()
        play.assert_not_called()
        self.assertFalse(self.core.c.player.is_playing)
        self.assertEqual(self.core.c.player.position, 130)

    def test_boundary_navigation_preserves_held_adjustment_until_release(self):
        for name, index, message in (('previous_track', 0, 'Początek książki.'),
                                     ('next_track', 2, 'Koniec książki.')):
            for kind, delta in (('seek_relative', 15), ('change_speed', .5),
                                ('change_volume', -1)):
                with self.subTest(name=name, kind=kind):
                    self.setUp()
                    c = self.core.c
                    c.start('abc', index)
                    c.player.position = 30
                    self.assertTrue(self.p.adjust(kind, delta))
                    self.jobs.pop(0)()
                    session = self.p._adjust_session
                    before_session = vars(session).copy()
                    before_player = vars(c.player).copy()
                    before_context = (c.generation, c.adjustment_context, c._adjustment)
                    saved = list(self.core.lib.saved)
                    self.core.messages.clear()
                    c.apply_adjustment = Mock(wraps=c.apply_adjustment)
                    c.check_timer = Mock(wraps=c.check_timer)
                    self.p.action(name)
                    self.assertEqual(vars(session), before_session,
                                     'UI must not release a boundary gesture')
                    while self.jobs:
                        self.jobs.pop(0)()
                    self.assertEqual(vars(session), before_session)
                    self.assertIs(self.p._adjust_session, session)
                    self.assertEqual(self.p._adjust_sessions, {kind: session})
                    self.assertEqual(vars(c.player), before_player)
                    self.assertEqual((c.generation, c.adjustment_context, c._adjustment), before_context)
                    self.assertEqual(self.core.lib.saved, saved)
                    c.apply_adjustment.assert_not_called()
                    c.check_timer.assert_not_called()
                    self.assertEqual(self.core.messages, [message])
                    self.assertFalse(self.p._busy)
                    self.p.end_adjustment()
                    while self.jobs:
                        self.jobs.pop(0)()
                    self.assertTrue(session.done)
                    self.assertTrue(c.player.is_playing)
                    self.assertEqual(self.p._adjust_sessions, {})

    def test_boundaries_do_not_poll_due_power_or_cancel_count_timer(self):
        import test_controller
        for name, index in (('previous_track', 0), ('next_track', 2)):
            for kind, delta in ((None, 0), ('seek_relative', 15),
                                ('change_speed', .5), ('change_volume', -1)):
                for mode in ('due-shutdown', 'due-sleep', 'count'):
                    with self.subTest(name=name, kind=kind, mode=mode):
                        self.setUp()
                        c = self.core.c
                        c.start('abc', index)
                        c.player.position = 30
                        if kind:
                            self.p.adjust(kind, delta)
                            self.jobs.pop(0)()
                        timer = test_controller.ControllerTests.real_timer(self.core)
                        if mode == 'count':
                            c.arm_timer(tracks=1, action='shutdown')
                        else:
                            c.arm_timer(minutes=1, action=mode.removeprefix('due-'))
                            self.core.now += 61
                        before = (c.timer_state(), c._timer_epoch, c.generation,
                                  c.adjustment_context, c.player.is_playing)
                        session = self.p._adjust_session
                        held = vars(session).copy() if session else None
                        c.check_timer = Mock(wraps=c.check_timer)
                        timer.poll = Mock(wraps=timer.poll)
                        c.player.play = Mock(wraps=c.player.play)
                        c.player.pause = Mock(wraps=c.player.pause)
                        self.core.lib.save_position = Mock(wraps=self.core.lib.save_position)
                        self.core.messages.clear()
                        self.p.action(name)
                        while self.jobs:
                            self.jobs.pop(0)()
                        self.assertEqual((c.timer_state(), c._timer_epoch, c.generation,
                                          c.adjustment_context, c.player.is_playing), before)
                        if session:
                            self.assertEqual(vars(session), held)
                        c.check_timer.assert_not_called()
                        timer.poll.assert_not_called()
                        c.player.play.assert_not_called()
                        c.player.pause.assert_not_called()
                        self.core.lib.save_position.assert_not_called()
                        self.assertEqual(self.core.powers, [])
                        self.assertEqual(self.core.messages,
                            ['Początek książki.' if index == 0 else 'Koniec książki.'])

    def test_boundary_keeps_pending_transition_and_stale_eof_generation(self):
        for name, index in (('previous_track', 0), ('next_track', 2)):
            with self.subTest(name=name):
                self.setUp()
                c = self.core.c
                c.start('abc', index)
                self.p.adjust('change_volume', -1)
                self.jobs.pop(0)()
                c.player.pause()
                # A transition token must survive even at the first boundary
                # (the last boundary also arises naturally after auto-advance).
                c._pending_transition = c.generation
                session = self.p._adjust_session
                before = (c.generation, c._pending_transition, vars(session).copy())
                saved = list(self.core.lib.saved)
                self.p.action(name)
                while self.jobs:
                    self.jobs.pop(0)()
                self.assertEqual((c.generation, c._pending_transition, vars(session)), before)
                self.assertEqual(self.core.lib.saved, saved)
                self.assertFalse(c.player.is_playing)

    def test_release_while_boundary_job_is_busy_is_applied_after_completion(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        session = self.p._adjust_session
        callbacks = []
        self.fixture.modules['wx'].CallAfter = lambda fn, *args: callbacks.append(lambda: fn(*args))
        self.p.action('previous_track')
        self.p.end_adjustment()  # Physical key-up, not navigation, finishes the hold.
        self.assertTrue(session.released)
        self.assertTrue(session.allow_resume)
        self.jobs.pop(0)()
        self.assertTrue(self.p._busy)
        self.assertIs(self.p._adjust_session, session)
        self.assertEqual(self.jobs, [])
        callbacks.pop(0)()
        self.assertFalse(self.p._busy)
        self.assertEqual(len(self.jobs), 1)
        self.jobs.pop(0)()
        callbacks.pop(0)()
        self.assertTrue(session.done)
        self.assertTrue(self.core.c.player.is_playing)
        self.assertEqual(self.p._adjust_sessions, {})

    def test_valid_navigation_finishes_latest_seek_without_old_audio_resume(self):
        import test_controller
        c = self.core.c
        test_controller.ControllerTests.real_timer(self.core)
        c.arm_timer(tracks=2, action='shutdown')
        c.player.callback('ended', None)  # Queued old-generation event, not new EOF.
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        self.p.adjust('seek_relative', 15)
        session = self.p._adjust_session
        plays = []
        play = c.player.play
        c.player.play = Mock(side_effect=lambda: (plays.append(c.track_index), play()))
        self.core.lib.saved.clear()
        callbacks = []
        self.fixture.modules['wx'].CallAfter = lambda fn, *args: callbacks.append(lambda: fn(*args))
        self.p.action('next_track')
        self.assertFalse(session.released)
        while self.jobs:
            self.jobs.pop(0)()
        # Worker owns session values, not UI maps/references.
        self.assertIs(self.p._adjust_session, session)
        self.assertIs(self.p._adjust_sessions['seek_relative'], session)
        self.assertEqual(plays, [1])
        self.assertEqual(self.core.lib.saved[0], ('abc', 0, 130))
        self.assertIsNone(c.timer_state())
        self.assertEqual(self.core.powers, [])
        while callbacks:
            callbacks.pop(0)()
        self.assertEqual(self.p._adjust_sessions, {})
        self.assertFalse(self.p._busy)
        while self.core.events:
            self.core.events.pop(0)()
        self.assertEqual(c.track_index, 1)
        self.assertEqual(plays, [1])

    def test_boundary_lookup_error_preserves_hold_and_announces_error(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        session = self.p._adjust_session
        before = vars(session).copy()
        self.core.lib.get_book = Mock(side_effect=OSError('synthetic read error'))
        self.p.action('previous_track')
        while self.jobs:
            self.jobs.pop(0)()
        self.assertEqual(vars(session), before)
        self.assertIs(self.p._adjust_session, session)
        self.assertFalse(self.p._busy)
        self.p._deliver_message.assert_called_with(
            'Nie udało się wykonać operacji. synthetic read error')

    def test_termination_during_navigation_track_load_cannot_restart(self):
        self.core.c.cancelled = lambda: self.p._closed
        original_load = self.core.c.player.load
        def terminating_load(*args, **kwargs):
            original_load(*args, **kwargs)
            self.p.terminate()
        self.core.c.player.load = terminating_load
        play = self.core.c.player.play = Mock(wraps=self.core.c.player.play)
        self.p.action('next_track')
        while self.jobs:
            self.jobs.pop(0)()
        play.assert_not_called()
        self.assertTrue(self.core.c.closed)

    def test_termination_during_navigation_adjustment_save_cannot_restart(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        original_save = self.core.lib.save_position
        def terminating_save(*args):
            original_save(*args)
            self.p.terminate()
        self.core.lib.save_position = terminating_save
        play = self.core.c.player.play = Mock(wraps=self.core.c.player.play)
        self.p.action('next_track')
        while self.jobs:
            self.jobs.pop(0)()
        play.assert_not_called()
        self.assertTrue(self.core.c.closed)

    def test_terminate_before_navigation_worker_cannot_resume_held_seek(self):
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        play = self.core.c.player.play = Mock(wraps=self.core.c.player.play)
        self.p.action('next_track')
        self.p.terminate()
        while self.jobs:
            self.jobs.pop(0)()
        play.assert_not_called()
        self.assertTrue(self.core.c.closed)
        self.assertFalse(self.p._busy)

    def test_removing_current_book_never_resumes_held_seek(self):
        wx = self.fixture.modules['wx']
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_WARNING, wx.YES = 1, 2, 4, 5103
        self.fixture.modules['gui'].messageBox = Mock(return_value=wx.YES)
        self.core.lib.books = Mock(return_value=[])
        self.p.adjust('seek_relative', 15)
        self.jobs.pop(0)()
        play = self.core.c.player.play = Mock(wraps=self.core.c.player.play)
        book = self.core.c.book_id
        self.p.remove_book(book)
        while self.jobs:
            self.jobs.pop(0)()
        play.assert_not_called()
        self.assertIsNone(self.core.c.book_id)

    def test_speed_limit_is_silent_noop_for_repeated_presses_and_hold(self):
        for speed, delta in ((2, .5), (1, -.5)):
            with self.subTest(speed=speed):
                self.setUp()
                c = self.core.c
                c.player.speed = speed
                play = c.player.play = Mock(wraps=c.player.play)
                pause = c.player.pause = Mock(wraps=c.player.pause)
                generation = c.generation
                saved = list(self.core.lib.saved)
                for _ in range(3):
                    self.p.action('change_speed', delta)
                    while self.jobs:
                        self.jobs.pop(0)()
                for _ in range(20):
                    self.assertTrue(self.p.adjust('change_speed', delta))
                    while self.jobs:
                        self.jobs.pop(0)()
                self.p.end_adjustment()
                while self.jobs:
                    self.jobs.pop(0)()
                pause.assert_not_called()
                play.assert_not_called()
                self.assertEqual(c.generation, generation)
                self.assertEqual(self.core.lib.saved, saved)
                self.assertEqual(c.player.speed, speed)
                self.p._say.assert_not_called()
                self.p._deliver_message.assert_not_called()
                # Reversing immediately must still work after hitting the limit.
                self.p.action('change_speed', -delta)
                while self.jobs:
                    self.jobs.pop(0)()
                self.assertEqual(c.player.speed, speed - delta)
                play.assert_called_once()

    def test_short_remove_question_and_result_and_no_default(self):
        wx = self.fixture.modules['wx']
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_WARNING = 1, 2, 4
        wx.YES, wx.NO = 5103, 5104
        gui = self.fixture.modules['gui']
        gui.messageBox = Mock(return_value=wx.NO)
        self.p.remove_book('abc')
        self.assertEqual(gui.messageBox.call_args.args[0], 'Usunąć kopię książki z biblioteki wraz z postępem i zakładkami?')
        self.assertTrue(gui.messageBox.call_args.args[2] & wx.NO_DEFAULT)
        self.assertEqual(self.jobs, [])
        gui.messageBox.return_value = wx.YES
        self.core.lib.books = Mock(return_value=[])
        self.p.remove_book('abc')
        self.jobs.pop(0)()
        self.assertEqual(self.core.lib.removed, 'abc')
        self.p._say.assert_called_once_with('Usunięto kopię książki z biblioteki.')


if __name__ == '__main__':
    unittest.main()
