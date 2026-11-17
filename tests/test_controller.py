import importlib.util
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

FILE = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/controller.py'


class Library:
    def __init__(self):
        self.book = {'id': 'abc', 'title': 'Książka', 'track_index': 1, 'position': 12.5,
                     'tracks': [{'title': '1'}, {'title': '2'}, {'title': '3'}]}
        self.saved = []
    def get_book(self, key):
        return self.book.copy()
    def track_path(self, key, index):
        return Path(str(index) + '.mp3')
    def track_duration(self, key, index):
        return 120
    def save_position(self, key, index, seconds):
        self.saved.append((key, index, seconds))
        self.book.update(track_index=index, position=seconds)
    def remove_book(self, key):
        self.removed = key


class Player:
    def __init__(self, callback):
        self.callback = callback
        self.position = 0
        self.duration = 120
        self.is_playing = False
        self.loaded = []
        self.volume = 100
        self.speed = 1.0
    def set_volume(self, value): self.volume = value
    def set_speed(self, value): self.speed = value
    def load(self, path, position=0, *, duration=None):
        self.loaded.append((path, position))
        self.position = position
        if duration is not None:
            self.duration = duration
    def play(self): self.is_playing = True
    def pause(self): self.is_playing = False
    def stop(self): self.is_playing = False
    def close(self): self.is_playing = False
    def seek(self, position): self.position = position


class Timer:
    def __init__(self):
        self.state = None
        self.due = None
        self.last_poll = None
    def arm(self, minutes=None, action='stop', target=None):
        self.state = dict(mode='end' if minutes is None else 'minutes',
                          action=action, target=target, minutes=minutes)
    def snapshot(self): return self.state
    def cancel(self):
        existed = self.state is not None
        self.state = None
        self.due = None
        return existed
    def poll(self, target=None, ended=False):
        self.last_poll = (target, ended)
        action, self.due = self.due, None
        if action: self.state = None
        return action


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(FILE.exists(), 'Brak kontrolera odtwarzania')
        spec = importlib.util.spec_from_file_location('book_controller_test', FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.lib = Library()
        self.pending = []
        self.messages = []
        self.c = module.Controller(self.lib, Player, self.pending.append, self.messages.append)

    def test_start_and_automatic_next_pass_library_duration_to_player(self):
        self.lib.track_duration = Mock(side_effect=lambda key, index: (60, 120, 180)[index])
        self.c.player.load = Mock(wraps=self.c.player.load)
        self.c.start('abc', index=0)
        self.c.player.load.assert_called_with(Path('0.mp3'), position=0, duration=60)
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.c.player.load.assert_called_with(Path('1.mp3'), position=0, duration=120)
        self.c.next_track()
        self.c.player.load.assert_called_with(Path('2.mp3'), position=0, duration=180)

    def test_automatic_notifications_use_separate_callback(self):
        self.assertIn('notify_auto', __import__('inspect').signature(type(self.c)).parameters)
        for ending in ('auto-off', 'final', 'timer-stop', 'timer-sleep', 'timer-shutdown'):
            with self.subTest(ending=ending):
                automatic = []
                self.c = type(self.c)(self.lib, Player, self.pending.append,
                    self.messages.append, notify_auto=automatic.append,
                    auto_next=lambda: False)
                self.real_timer()
                index = 2 if ending == 'final' else 0
                self.c.start('abc', index=index)
                if ending.startswith('timer-'):
                    self.c.arm_timer(tracks=1, action=ending.removeprefix('timer-'))
                self.messages.clear()
                self.c.player.position = self.c.player.duration
                self.c.player.callback('ended', None)
                self.c.player.callback('ended', None)
                while self.pending:
                    self.pending.pop(0)()
                expected = {'auto-off': ['Koniec odcinka 1.'],
                            'final': ['Koniec książki.'],
                            'timer-stop': ['Wyłącznik czasowy: zatrzymano książkę.'],
                            'timer-sleep': [], 'timer-shutdown': []}[ending]
                self.assertEqual(automatic, expected)
                self.assertEqual(self.messages, [])
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.lib.saved[-1], ('abc', index, 120))
                self.assertEqual(self.powers,
                    [ending.removeprefix('timer-')] if ending in ('timer-sleep', 'timer-shutdown') else [])

    def test_manual_messages_and_errors_do_not_use_automatic_callback(self):
        self.assertIn('notify_auto', __import__('inspect').signature(type(self.c)).parameters)
        automatic = []
        self.c = type(self.c)(self.lib, Player, self.pending.append,
            self.messages.append, notify_auto=automatic.append)
        self.real_timer()
        self.c.start('abc', index=0)
        self.c.previous_track()
        self.c.toggle()
        self.c.toggle()
        self.c.arm_timer(minutes=1)
        self.c.cancel_timer()
        self.c.player.callback('error', 'synthetic failure')
        self.pending.pop(0)()
        self.assertEqual(self.messages, ['Odcinek 1.', 'Początek książki.',
            'Pauza.', 'Wznowiono odtwarzanie.', 'Wyłącznik czasowy włączony.',
            'Wyłącznik czasowy anulowany.', 'Błąd odtwarzania: synthetic failure'])
        self.c.start('abc', index=2)
        self.c.next_track()
        self.c.player.position = 120
        self.c.start('abc')
        self.assertEqual(self.messages[-3:], ['Odcinek 3.', 'Koniec książki.', 'Koniec książki.'])
        self.assertEqual(automatic, [])

    def real_timer(self):
        spec = importlib.util.spec_from_file_location('controller_sleep_test', FILE.with_name('sleep_timer.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.now = 100.0
        self.c.sleep_timer = module.SleepTimer(clock=lambda: self.now)
        self.powers = []
        self.c.power = self.powers.append
        return self.c.sleep_timer

    def test_track_count_fires_at_nth_natural_end_only(self):
        self.assertIn('tracks', __import__('inspect').signature(self.c.arm_timer).parameters)
        self.real_timer()
        self.c.start('abc', index=0)
        self.c.arm_timer(tracks=2, action='shutdown')
        for index in (0, 1):
            self.c.player.position = 120
            self.c.player.callback('ended', None)
            self.c.player.callback('ended', None)
            while self.pending:
                self.pending.pop(0)()
            if index == 0:
                self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)
                self.assertEqual(self.powers, [])
        self.assertEqual(self.powers, ['shutdown'])
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(len(self.c.player.loaded), 2)
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 1, 120))
        self.assertIsNone(self.c.timer_state())

    def test_count_validation_and_manual_changes_cancel_but_seek_keeps(self):
        timer = self.real_timer()
        with self.assertRaises(ValueError):
            self.c.arm_timer(tracks=1)
        self.c.start('abc', index=1)
        self.c.arm_timer(minutes=15)
        before = timer.snapshot()
        for value in (0, True, '1', 1.5, 3):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'odcink'):
                    self.c.arm_timer(tracks=value)
                self.assertEqual(timer.snapshot(), before)
        for change in (lambda: self.c.next_track(), lambda: self.c.previous_track(),
                       lambda: self.c.start('other', index=1), lambda: self.c.remove('abc')):
            self.c.start('abc', index=1)
            self.c.arm_timer(tracks=2)
            self.c.seek_relative(10)
            self.assertEqual(timer.snapshot()['remaining_tracks'], 2)
            change()
            self.assertIsNone(timer.snapshot())
            self.assertIn('Wyłącznik czasowy anulowany.', self.messages)

    def test_count_temporarily_enables_auto_next_without_changing_preference(self):
        self.real_timer()
        self.c.auto_next = lambda: False
        self.c.start('abc', index=0)
        self.c.arm_timer(tracks=2, action='shutdown')
        self.c.player.position = 120
        for _ in range(2):
            self.c.player.callback('ended', None)
        while self.pending:
            self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 1)
        self.assertTrue(self.c.player.is_playing)
        self.assertFalse(self.c.auto_next())
        self.assertTrue(self.c.effective_auto_next())
        self.assertEqual(self.powers, [])
        self.assertEqual(self.c.timer_state()['remaining_tracks'], 1)

    def test_timer_status_uses_one_snapshot_polish_units_and_actions(self):
        self.assertTrue(hasattr(self.c, 'timer_status'))
        self.assertEqual(self.c.timer_status(), 'Wyłącznik czasowy nieaktywny.')
        self.real_timer()
        self.c.start('abc', index=0)
        for action, label in [('stop', 'zatrzymanie odtwarzania'),
                              ('shutdown', 'wyłączenie komputera'),
                              ('sleep', 'przełączenie w stan uśpienia')]:
            self.c.arm_timer(minutes=184, action=action)
            self.assertEqual(self.c.timer_status(),
                'Wyłącznik czasowy aktywny. Za 3 godziny i 4 minuty ' + label + '.')
            self.c.arm_timer(action=action)
            self.assertEqual(self.c.timer_status(),
                'Wyłącznik czasowy aktywny. Po zakończeniu bieżącego odcinka ' + label + '.')
            for count in (1, 2, 3):
                self.c.arm_timer(tracks=count, action=action)
                self.assertEqual(self.c.timer_status(),
                    'Wyłącznik czasowy aktywny. Po zakończeniu {} {} {}.'.format(
                        count, 'odcinka' if count == 1 else 'odcinków', label))
        self.c.arm_timer(minutes=61)
        self.assertEqual(self.c.timer_status(),
            'Wyłącznik czasowy aktywny. Za 1 godzinę i 1 minutę zatrzymanie odtwarzania.')
        self.c.cancel_timer()
        self.assertEqual(self.messages[-1], 'Wyłącznik czasowy anulowany.')
        self.assertEqual(self.c.timer_status(), 'Wyłącznik czasowy nieaktywny.')

    def test_ui_cancel_during_consumed_timer_save_prevents_power(self):
        self.real_timer()
        self.c.start('abc')
        self.c.arm_timer(minutes=1, action='shutdown')
        self.now += 60
        entered, release = threading.Event(), threading.Event()
        original_save = self.lib.save_position

        def slow_save(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('save not released')
            original_save(*args)

        self.lib.save_position = slow_save
        with ThreadPoolExecutor(max_workers=2) as executor:
            checking = executor.submit(self.c.check_timer)
            try:
                self.assertTrue(entered.wait(5))
                executor.submit(self.c.cancel_timer).result(timeout=1)
                self.assertIsNone(executor.submit(self.c.timer_state).result(timeout=1))
            finally:
                release.set()
            self.assertTrue(checking.result(timeout=5))
        self.assertEqual(self.powers, [])

    def test_sleep_action_precedes_next_part_and_saves_before_power_once(self):
        self.assertTrue(hasattr(self.c, 'arm_timer'))
        for action in ('stop', 'sleep', 'shutdown'):
            with self.subTest(action=action):
                timer = Timer()
                powers = []
                self.c = type(self.c)(self.lib, Player, self.pending.append, self.messages.append,
                    sleep_timer=timer, power=lambda action: powers.append((action,
                        self.c.player.is_playing, self.lib.saved[-1])))
                self.c.start('abc', index=0)
                self.c.arm_timer(None, action)
                self.assertEqual(timer.state['target'], ('abc', 0))
                self.c.player.position = 120
                timer.due = action
                self.c.player.callback('ended', None)
                self.pending.pop(0)()
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.c.track_index, 0)
                self.assertEqual(len(self.c.player.loaded), 1)
                self.assertEqual(self.lib.saved[-1], ('abc', 0, 120))
                self.c.check_timer()
                self.assertEqual(powers, [] if action == 'stop' else [(action, False, ('abc', 0, 120))])
                self.assertIsNone(timer.state)

    def test_only_stop_action_announces_timer_execution(self):
        for action in ('stop', 'sleep', 'shutdown'):
            with self.subTest(action=action):
                self.setUp()
                self.real_timer()
                self.c.start('abc', index=0)
                self.c.player.position = 35
                self.c.arm_timer(minutes=1, action=action)
                self.messages.clear()
                self.now += 60
                observed = []
                self.c.power = lambda value: observed.append((value,
                    self.c.player.is_playing, self.lib.saved[-1], list(self.messages)))
                self.assertTrue(self.c.check_timer())
                self.assertFalse(self.c.check_timer())
                self.assertFalse(self.c.player.is_playing)
                self.assertEqual(self.lib.saved[-1], ('abc', 0, 35))
                self.assertIsNone(self.c.timer_state())
                self.assertEqual(self.messages,
                    ['Wyłącznik czasowy: zatrzymano książkę.'] if action == 'stop' else [])
                self.assertEqual(observed,
                    [] if action == 'stop' else [(action, False, ('abc', 0, 35), [])])

    def at_power_commit(self, callback):
        original_lock = self.c._power_lock

        @contextmanager
        def barrier():
            self.c._power_lock = original_lock  # close may reenter the real lock
            with original_lock:
                callback()
                yield

        self.c._power_lock = barrier()

    def test_close_during_expired_timer_prevents_power(self):
        for stage in ('poll', 'pause', 'save', 'power-commit'):
            with self.subTest(stage=stage):
                self.setUp()
                timer, powers = Timer(), []
                self.c.sleep_timer, self.c.power = timer, powers.append
                self.c.start('abc')
                self.c.arm_timer(15, 'shutdown')
                timer.due = 'shutdown'
                if stage == 'power-commit':
                    self.at_power_commit(self.c.close)
                    self.assertTrue(self.c.check_timer())
                    self.assertTrue(self.c.closed)
                    self.assertEqual(powers, [])
                    continue
                owner, name = {'poll': (timer, 'poll'),
                    'pause': (self.c.player, 'pause'),
                    'save': (self.lib, 'save_position')}[stage]
                original = getattr(owner, name)

                def close_at_barrier(*args, **kwargs):
                    setattr(owner, name, original)  # close also pauses/saves
                    result = original(*args, **kwargs)
                    self.c.close()
                    return result

                setattr(owner, name, close_at_barrier)
                self.assertTrue(self.c.check_timer())
                self.assertTrue(self.c.closed)
                self.assertEqual(powers, [])

    def test_injected_cancellation_is_checked_at_entry_and_after_blocking_work(self):
        for action in ('sleep', 'shutdown'):
            for stage in ('before-poll', 'pause', 'save', 'power-commit'):
                with self.subTest(action=action, stage=stage):
                    cancelled, powers = [False], []
                    timer = Timer()
                    self.c = type(self.c)(self.lib, Player, self.pending.append,
                        self.messages.append, sleep_timer=timer, power=powers.append,
                        cancelled=lambda: cancelled[0])
                    self.c.start('abc')
                    self.c.arm_timer(15, action)
                    timer.due = action
                    if stage == 'before-poll':
                        cancelled[0] = True
                    elif stage == 'power-commit':
                        self.at_power_commit(lambda: cancelled.__setitem__(0, True))
                    else:
                        owner, name = {'pause': (self.c.player, 'pause'),
                            'save': (self.lib, 'save_position')}[stage]
                        original = getattr(owner, name)

                        def cancel(*args, **kwargs):
                            setattr(owner, name, original)
                            result = original(*args, **kwargs)
                            cancelled[0] = True
                            return result

                        setattr(owner, name, cancel)
                    self.assertEqual(self.c.check_timer(), stage != 'before-poll')
                    self.assertEqual(powers, [])
                    self.assertFalse(self.c.closed, 'external signal precedes worker close')
                    if stage == 'before-poll':
                        self.assertIsNone(timer.last_poll)
                    else:
                        self.assertIsNone(timer.state)

    def test_close_can_signal_while_timer_save_is_blocked(self):
        timer, powers = Timer(), []
        self.c.sleep_timer, self.c.power = timer, powers.append
        self.c.start('abc')
        self.c.arm_timer(15, 'sleep')
        timer.due = 'sleep'
        entered, release = threading.Event(), threading.Event()
        original_save = self.lib.save_position

        def block_once(*args):
            self.lib.save_position = original_save
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release save')
            original_save(*args)

        self.lib.save_position = block_once
        with ThreadPoolExecutor(max_workers=2) as executor:
            checking = executor.submit(self.c.check_timer)
            try:
                self.assertTrue(entered.wait(5))
                executor.submit(self.c.close).result(timeout=2)
                self.assertTrue(self.c.closed)
            finally:
                release.set()
            self.assertTrue(checking.result(timeout=5))
        self.assertEqual(powers, [])

    def test_timer_save_failure_never_executes_power(self):
        self.assertTrue(hasattr(self.c, 'check_timer'))
        timer, powers = Timer(), []
        self.c.sleep_timer, self.c.power = timer, powers.append
        self.c.start('abc')
        self.c.arm_timer(15, 'shutdown')
        timer.due = 'shutdown'
        def fail(*args): raise OSError('disk failure')
        self.lib.save_position = fail
        with self.assertRaises(OSError): self.c.check_timer()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(powers, [])
        self.assertIsNone(timer.state)

    def test_timer_cancels_pending_transition_and_clears_on_close(self):
        self.assertTrue(hasattr(self.c, 'arm_timer'))
        _, valid, resume = self.prepare_transition()
        timer = Timer()
        self.c.sleep_timer = timer
        self.c.arm_timer(15, 'stop')
        timer.due = 'stop'
        self.c.check_timer()
        resume()
        self.assertFalse(valid())
        self.assertFalse(self.c.player.is_playing)
        self.c.arm_timer(15, 'shutdown')
        self.c.close()
        self.assertIsNone(timer.state)

    def test_end_timer_cancelled_on_manual_part_change_but_minutes_survives(self):
        self.assertTrue(hasattr(self.c, 'arm_timer'))
        self.c.sleep_timer = Timer()
        with self.assertRaises(ValueError): self.c.arm_timer(None, 'sleep')
        self.c.start('abc', index=0)
        self.c.arm_timer(None, 'sleep')
        self.c.next_track()
        self.assertIsNone(self.c.sleep_timer.state)
        self.c.arm_timer(15, 'sleep')
        self.c.start('other')
        self.assertIsNotNone(self.c.sleep_timer.state)
        self.c.cancel_timer()
        self.assertIsNone(self.c.sleep_timer.state)

    def test_start_and_manual_navigation_announce_only_ordinal(self):
        self.lib.book['title'] = 'Long private book title'
        self.lib.book['tracks'][1]['title'] = 'Long private filename'
        self.c.start('abc', index=0)
        self.c.next_track()
        self.c.previous_track()
        self.assertEqual(self.messages, ['Odcinek 1.', 'Odcinek 2.', 'Odcinek 1.'])

    def test_auto_next_off_announces_current_ordinal_only(self):
        self.c.auto_next = lambda: False
        self.c.start('abc', index=1)
        self.messages.clear()
        self.c.player.position = 120
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.assertEqual(self.messages, ['Koniec odcinka 2.'])

    def test_auto_next_off_stops_at_current_end_and_toggle_cancels_pending(self):
        self.c.auto_next = lambda: False
        self.c.start('abc', index=0)
        self.c.player.position = 120
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 0)
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 0, 120))
        _, is_current, resume = self.prepare_transition(index=0)
        self.c.auto_next = lambda: False
        self.c.auto_next_changed(False)
        resume()
        self.assertFalse(is_current())
        self.assertFalse(self.c.player.is_playing)

    def test_volume_and_speed_only_when_playing_preserve_speed_for_other_book(self):
        self.assertTrue(hasattr(self.c, 'change_volume'))
        self.assertTrue(hasattr(self.c, 'change_speed'))
        self.c.change_volume(-1)
        self.c.change_speed(.5)
        self.assertEqual(self.messages, [])
        self.c.start('abc')
        self.c.change_volume(-1)
        self.assertEqual(self.c.player.volume, 99)
        self.assertEqual(self.messages[-1], 'Głośność 99 procent.')
        self.c.change_speed(.5)
        self.assertEqual(self.c.player.speed, 1.5)
        self.assertEqual(self.messages[-1], 'Prędkość 1,5×')
        self.c.next_track()
        self.assertEqual(self.c.player.speed, 1.5)
        self.c.toggle()
        self.c.change_speed(.5)
        self.c.change_volume(-1)
        self.assertEqual((self.c.player.speed, self.c.player.volume), (1.5, 99))
        self.c.start('other')
        self.assertEqual(self.c.player.speed, 1.5)
        self.c.change_speed(-.5)
        self.messages.clear()
        self.c.change_speed(-.5)
        self.c.change_speed(-.5)
        self.assertEqual(self.c.player.speed, 1)
        self.assertEqual(self.messages, [])
        self.c.change_speed(.5)
        self.assertEqual(self.messages[-1], 'Prędkość 1,5×')

    def test_live_volume_keeps_playback_and_generation_unchanged(self):
        self.c.start('abc')
        self.c.player.position = 42.5
        generation = self.c.generation
        self.c.player.pause = Mock(wraps=self.c.player.pause)
        self.c.player.play = Mock(wraps=self.c.player.play)
        self.c.change_volume(-1)
        self.assertEqual(self.c.player.volume, 99)
        self.c.player.pause.assert_not_called()
        self.c.player.play.assert_not_called()
        self.assertEqual(self.c.generation, generation)
        self.assertEqual(self.c.player.position, 42.5)
        self.assertTrue(self.c.player.is_playing)

    def test_setting_racing_eof_handles_end_once_without_replaying(self):
        for name, delta in (('change_volume', -1), ('change_speed', .5)):
            for ending in ('auto-next', 'auto-next-off', 'final', 'stop', 'sleep', 'shutdown'):
                with self.subTest(setting=name, ending=ending):
                    self.setUp()
                    transitions, powers, plays = [], [], []
                    self.c.transition = lambda *args: transitions.append(args)
                    self.c.auto_next = lambda: ending != 'auto-next-off'
                    index = 2 if ending == 'final' else 0
                    self.c.start('abc', index=index)
                    self.c.player.position = 119
                    self.lib.saved.clear()
                    self.messages.clear()
                    if ending in ('stop', 'sleep', 'shutdown'):
                        self.c.sleep_timer = Timer()
                        self.c.power = powers.append  # never real system power
                        self.c.arm_timer(None, ending)
                        self.c.sleep_timer.due = ending
                    original_pause = self.c.player.pause
                    original_play = self.c.player.play

                    def eof_inside_pause():
                        self.c.player.pause = original_pause
                        self.c.player.position = self.c.player.duration
                        self.c.player.is_playing = False
                        # Already queued before pause returns, including a duplicate.
                        self.c.player.callback('ended', None)
                        self.c.player.callback('ended', None)
                        original_pause()

                    def play_with_backend_rewind():
                        plays.append((self.c.track_index, self.c.player.position))
                        if self.c.player.position >= self.c.player.duration:
                            self.c.player.position = 0
                        original_play()

                    if name == 'change_speed':
                        self.c.player.pause = eof_inside_pause
                    else:
                        original_set_volume = self.c.player.set_volume
                        def live_volume_racing_end(value):
                            eof_inside_pause()
                            original_set_volume(value)
                        self.c.player.set_volume = live_volume_racing_end
                    self.c.player.play = play_with_backend_rewind
                    getattr(self.c, name)(delta)
                    self.assertEqual(len(self.pending), 2, 'exercise EOF in pause')
                    while self.pending:
                        self.pending.pop(0)()
                    self.assertEqual(plays, [], 'setting must not replay completed part')
                    self.assertEqual(self.lib.saved.count(('abc', index, 120)), 1)
                    if ending == 'auto-next':
                        self.assertEqual(self.c.track_index, 1)
                        self.assertEqual(len(transitions), 1)
                        _, valid, resume = transitions[0]
                        self.assertTrue(valid())
                        resume()
                        resume()
                        self.assertEqual(plays, [(1, 0)])
                    else:
                        self.assertEqual(self.c.track_index, index)
                        self.assertFalse(self.c.player.is_playing)
                        self.assertEqual(transitions, [])
                        self.assertEqual(self.lib.saved, [('abc', index, 120)])
                        if ending in ('stop', 'sleep', 'shutdown'):
                            self.assertEqual(self.c.sleep_timer.last_poll, (('abc', index), True))
                            self.assertIsNone(self.c.timer_state())
                            self.c.check_timer()
                            self.assertEqual(powers, [ending] if ending != 'stop' else [])
                        else:
                            self.assertEqual(sum(msg.startswith('Koniec ') for msg in self.messages), 1)

    def test_setting_clamped_to_current_value_does_not_restart(self):
        for name, value, delta in (('volume', 100, 1), ('volume', 0, -1),
                                   ('speed', 2.0, .5), ('speed', 1.0, -.5)):
            with self.subTest(setting=name, value=value):
                self.setUp()
                self.c.start('abc')
                setattr(self.c.player, name, value)
                self.c.player.position = 42.5
                generation = self.c.generation
                self.c.player.pause = Mock(wraps=self.c.player.pause)
                self.c.player.play = Mock(wraps=self.c.player.play)
                getattr(self.c, 'change_' + name)(delta)
                self.c.player.pause.assert_not_called()
                self.c.player.play.assert_not_called()
                self.assertEqual(self.c.generation, generation)
                self.assertEqual(self.c.player.position, 42.5)
                self.assertEqual(self.lib.saved, [])

    def test_changed_setting_resumes_and_saves_position_from_pause_barrier(self):
        for name, delta, value in (('speed', .5, 1.5),):
            with self.subTest(setting=name):
                self.setUp()
                self.c.start('abc')
                self.c.player.position = 42.5
                original_pause = self.c.player.pause
                original_play = self.c.player.play
                plays = []

                def pause():
                    self.c.player.position = 43.75
                    original_pause()

                def play():
                    plays.append(self.c.player.position)
                    original_play()

                self.c.player.pause = pause
                self.c.player.play = play
                getattr(self.c, 'change_' + name)(delta)
                self.assertEqual(getattr(self.c.player, name), value)
                self.assertEqual(plays, [43.75])
                self.assertTrue(self.c.player.is_playing)
                self.assertEqual(self.lib.saved, [('abc', 1, 43.75)])
                self.assertEqual(len(self.c.player.loaded), 1)

    def test_toggle_after_restart_starts_selected_saved_place(self):
        self.c.toggle(selected_book_id='abc')
        self.assertEqual(self.c.book_id, 'abc')
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.loaded, [(Path('1.mp3'), 12.5)])
        self.assertTrue(self.c.player.is_playing)

    def test_resume_saved_track_and_time(self):
        self.c.start('abc')
        self.assertEqual(self.c.player.loaded, [(Path('1.mp3'), 12.5)])
        self.assertTrue(self.c.player.is_playing)
        self.c.player.position = 20
        self.c.toggle()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 1, 20))

    def test_toggle_uses_active_book_not_other_selection(self):
        self.c.start('abc')
        self.c.toggle(selected_book_id='other')
        self.assertEqual(self.c.book_id, 'abc')
        self.assertFalse(self.c.player.is_playing)
        self.c.toggle(selected_book_id='other')
        self.assertEqual(self.c.book_id, 'abc')
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(len(self.c.player.loaded), 1)

    def prepare_transition(self, index=1):
        self.transitions = []
        self.c = type(self.c)(self.lib, Player, self.pending.append, self.messages.append,
            transition=lambda *args: self.transitions.append(args))
        self.lib.book['tracks'][2]['title'] = 'Rozdział 03 – Powrót'
        self.c.start('abc', index=index)
        self.c.player.position = self.c.player.duration
        self.messages.clear()
        self.lib.saved.clear()
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.assertEqual(len(self.transitions), 1)
        return self.transitions[0]

    def test_eof_prepares_numbered_announcement_then_waits_for_resume(self):
        message, is_current, resume = self.prepare_transition()
        self.assertEqual(message, 'Odcinek 3.')
        self.assertEqual(self.c.book_id, 'abc')
        self.assertEqual(self.c.track_index, 2)
        self.assertEqual(self.c.player.loaded[-1], (Path('2.mp3'), 0))
        self.assertEqual(self.lib.saved, [('abc', 1, 120), ('abc', 2, 0)])
        self.assertFalse(self.c.player.is_playing)
        self.assertTrue(is_current())
        self.assertEqual(self.messages, [])
        resume()
        self.assertFalse(is_current())
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.messages, [])
        self.c.player.pause()
        resume()
        self.assertFalse(self.c.player.is_playing, 'resume must be one-shot')

    def test_toggle_in_gap_cancels_then_next_toggle_resumes_prepared_track(self):
        _, is_current, resume = self.prepare_transition()
        self.c.toggle(selected_book_id='other')
        self.assertFalse(is_current())
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.c.track_index, 2)
        self.assertEqual(self.lib.saved[-1], ('abc', 2, 0))
        resume()
        self.assertFalse(self.c.player.is_playing)
        self.c.toggle(selected_book_id='other')
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.c.player.position, 0)
        self.assertEqual(len(self.c.player.loaded), 2)

    def test_manual_actions_cancel_transition_without_late_playback(self):
        actions = {
            'seek': lambda: self.c.seek_relative(10),
            'start': lambda: self.c.start('other', index=0),
            'next': lambda: self.c.next_track(),
            'previous': lambda: self.c.previous_track(),
            'remove': lambda: self.c.remove('abc'),
            'remove_other': lambda: self.c.remove('other'),
            'close': lambda: self.c.close(),
        }
        for action in actions:
            with self.subTest(action=action):
                self.setUp()
                _, is_current, resume = self.prepare_transition(index=0)
                # Duplicate EOF already queued in the gap must remain stale
                # even when a command cancels without loading another track.
                self.c.player.callback('ended', None)
                actions[action]()
                self.assertFalse(is_current())
                before = (self.c.book_id, self.c.track_index, self.c.player.position,
                          self.c.player.is_playing, list(self.c.player.loaded))
                while self.pending:
                    self.pending.pop(0)()
                resume()
                self.assertEqual(before, (self.c.book_id, self.c.track_index,
                    self.c.player.position, self.c.player.is_playing, self.c.player.loaded))
                if not self.c.closed and self.c.book_id is not None:
                    was_playing = self.c.player.is_playing
                    self.c.toggle()
                    self.assertEqual(self.c.player.is_playing, not was_playing)

    def test_track_boundary_command_preserves_pending_transition(self):
        _, is_current, resume = self.prepare_transition()
        generation = self.c.generation
        self.c.player.callback('ended', None)
        self.c.next_track()
        self.assertTrue(is_current())
        self.assertEqual(self.c.generation, generation)
        self.pending.pop(0)()
        self.assertEqual(self.messages, ['Koniec książki.'])
        self.assertFalse(self.c.player.is_playing)
        resume()
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.c.player.position, 0)

    def test_boundary_navigation_changes_nothing_except_manual_message(self):
        for index, command, expected in ((0, 'previous_track', 'Początek książki.'),
                                          (2, 'next_track', 'Koniec książki.')):
            for state in ('playing', 'paused', 'adjusting'):
                with self.subTest(index=index, state=state):
                    self.setUp()
                    self.real_timer()
                    self.c.start('abc', index=index)
                    self.c.player.position = 42.5
                    self.c.player.speed = 1.5
                    self.c.arm_timer(tracks=1)
                    session = None
                    if state == 'paused':
                        self.c.toggle()
                    elif state == 'adjusting':
                        session = self.c.new_adjustment('seek_relative')
                        session.move(10)
                        self.c.apply_adjustment(session)
                    self.messages.clear()
                    before = (self.c.player.position, self.c.player.speed,
                              self.c.player.is_playing, self.c.generation,
                              self.c.adjustment_context, self.c.timer_state(),
                              list(self.lib.saved), list(self.c.player.loaded))
                    self.c.player.pause = Mock(wraps=self.c.player.pause)
                    self.c.player.play = Mock(wraps=self.c.player.play)
                    getattr(self.c, command)()
                    self.assertEqual(self.messages, [expected])
                    self.assertEqual(before, (self.c.player.position, self.c.player.speed,
                        self.c.player.is_playing, self.c.generation,
                        self.c.adjustment_context, self.c.timer_state(),
                        self.lib.saved, self.c.player.loaded))
                    self.c.player.pause.assert_not_called()
                    self.c.player.play.assert_not_called()
                    if session:
                        self.assertFalse(session.cancelled)
                        session.release()
                        self.c.apply_adjustment(session)
                        self.assertTrue(self.c.player.is_playing)

    def test_duplicate_eof_during_gap_cannot_schedule_or_skip(self):
        _, is_current, resume = self.prepare_transition(index=0)
        self.c.player.callback('ended', None)
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.assertTrue(is_current())
        self.assertEqual(len(self.transitions), 1)
        self.assertEqual(self.c.track_index, 1)
        resume()
        self.pending.pop(0)()
        self.assertEqual(len(self.transitions), 1)
        self.assertEqual(self.c.track_index, 1)
        self.assertTrue(self.c.player.is_playing)

    def test_duplicate_queued_eof_advances_once(self):
        self.c.start('abc', index=0)
        self.c.player.callback('ended', None)
        self.c.player.callback('ended', None)
        while self.pending:
            self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(len(self.c.player.loaded), 2)

    def test_duplicate_final_eof_is_announced_once(self):
        self.c.start('abc', index=2)
        self.messages.clear()
        self.c.player.callback('ended', None)
        self.c.player.callback('ended', None)
        while self.pending:
            self.pending.pop(0)()
        self.assertEqual(self.messages,
            ['Koniec książki.'])
        self.assertFalse(self.c.player.is_playing)

    def test_natural_end_advances_then_keeps_completed_book(self):
        self.c.start('abc')
        self.c.player.callback('ended', None)
        self.assertEqual(self.c.track_index, 1)
        self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 2)
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 2, 120))
        self.assertFalse(hasattr(self.lib, 'removed'))

    def test_stale_end_does_not_skip_new_selection(self):
        self.c.start('abc')
        self.c.player.callback('ended', None)
        self.c.start('abc', index=0)
        self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 0)

    def test_seek_clamped_pause_close_and_delete(self):
        self.c.start('abc')
        self.c.seek_relative(-99)
        self.assertEqual(self.c.player.position, 0)
        self.c.seek_relative(999)
        self.assertEqual(self.c.player.position, 120)
        self.c.previous_track()
        self.assertEqual(self.c.track_index, 0)
        self.c.next_track()
        self.assertEqual(self.c.track_index, 1)
        self.c.remove('abc')
        self.assertEqual(self.lib.removed, 'abc')
        self.assertIsNone(self.c.book_id)
        self.c.close()
        self.assertTrue(self.c.closed)

    def test_old_track_finishing_during_new_probe_cannot_skip_new_track(self):
        original_load = self.c.player.load
        def finish_old_while_probing(path, position=0, *, duration=None):
            if self.c.player.is_playing:
                self.c.player.callback('ended', None)
            original_load(path, position, duration=duration)
        self.c.player.load = finish_old_while_probing
        self.c.start('abc')
        self.c.start('abc', index=0)
        for callback in self.pending:
            callback()
        self.assertEqual(self.c.track_index, 0)

    def test_eof_in_seek_gap_cannot_override_seek(self):
        self.c.start('abc')
        self.c.player.position = 30
        original_seek = self.c.player.seek

        def eof_before_seek_barrier(position):
            # The old backend can finish before seek acquires its command lock.
            if self.c.player.is_playing:
                self.c.player.is_playing = False
                self.c.player.position = self.c.player.duration
                self.c.player.callback('ended', None)
            original_seek(position)

        self.c.player.seek = eof_before_seek_barrier
        self.c.seek_relative(-10)
        while self.pending:
            self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.position, 20)
        self.assertTrue(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 1, 20))
        self.assertEqual(len(self.c.player.loaded), 1)

    def test_eof_delivered_inside_pause_keeps_old_generation(self):
        self.c.start('abc')
        self.c.player.position = 30
        original_pause = self.c.player.pause

        def eof_before_pause_barrier():
            self.c.player.callback('ended', None)
            original_pause()

        self.c.player.pause = eof_before_pause_barrier
        self.c.seek_relative(-10)
        self.assertEqual(len(self.pending), 1, 'exercise the callback barrier')
        self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.c.player.position, 20)
        self.assertTrue(self.c.player.is_playing)

    def test_seek_preserves_playing_or_paused_state_except_at_end(self):
        for was_playing in (True, False):
            for delta, expected in ((-99, 0), (10, 40), (90, 120), (999, 120)):
                with self.subTest(was_playing=was_playing, delta=delta):
                    self.c.start('abc', index=1)
                    self.c.player.position = 30
                    if not was_playing:
                        self.c.toggle()
                    self.c.seek_relative(delta)
                    self.assertEqual(self.c.player.position, expected)
                    self.assertEqual(self.c.player.is_playing,
                                     was_playing and expected < 120)
                    self.assertEqual(self.c.track_index, 1)
                    self.assertEqual(self.lib.saved[-1], ('abc', 1, expected))
                    self.assertEqual(self.pending, [])

    def test_seek_without_active_book_is_also_silent(self):
        self.c.seek_relative(10)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.lib.saved, [])

    def test_seek_is_silent_and_saves_position(self):
        self.c.start('abc')
        self.messages.clear()
        self.c.seek_relative(10)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.lib.saved[-1], ('abc', 1, 22.5))

    def test_status_divides_source_remaining_by_current_speed_before_formatting(self):
        self.c.start('abc', index=1)
        self.c.player.position = 30
        for speed, expected in ((1.0, '1 minuta i 30 sekund'), (1.5, '1 minuta'),
                                (2.0, '45 sekund')):
            with self.subTest(speed=speed):
                self.c.player.speed = speed
                self.assertEqual(self.c.status(), expected + ' do końca odcinka.')
                self.c.player.pause()
                self.assertEqual(self.c.status(), expected + ' do końca odcinka.')
                self.assertEqual(self.c.player.position, 30)
                self.assertEqual(self.c.player.duration, 120)
        self.c.player.position = 121
        self.assertEqual(self.c.status(), 'Koniec odcinka.')
        self.c.player.duration = 2 * (3 * 3600 + 4 * 60)
        self.c.player.position = 0
        self.assertEqual(self.c.status(), '3 godziny i 4 minuty do końca odcinka.')

    def test_status_reports_only_remaining_in_current_part(self):
        self.assertEqual(self.c.status(), 'Nie wybrano książki.')
        self.c.start('abc')
        self.c.player.position = 30
        expected = '1 minuta i 30 sekund do końca odcinka.'
        self.assertEqual(self.c.status(), expected)
        self.c.toggle()
        self.assertEqual(self.c.status(), expected)
        self.c.player.position = 120
        self.assertEqual(self.c.status(), 'Koniec odcinka.')
        self.c.player.position = 121
        self.assertEqual(self.c.status(), 'Koniec odcinka.')
        self.c.player.duration = 3 * 3600 + 4 * 60
        self.c.player.position = 0
        self.assertEqual(self.c.status(), '3 godziny i 4 minuty do końca odcinka.')

    def test_error_during_transition_cancels_delayed_resume(self):
        _, is_current, resume = self.prepare_transition()
        self.c.player.callback('error', 'awaria')
        self.pending.pop(0)()
        self.assertFalse(is_current())
        resume()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.c.track_index, 2)
        self.assertEqual(self.lib.saved[-1], ('abc', 2, 0))
        self.assertEqual(self.messages, ['Błąd odtwarzania: awaria'])

    def test_transition_scheduler_error_invalidates_captured_callbacks(self):
        self.transitions = []
        def fail_schedule(*args):
            self.transitions.append(args)
            raise RuntimeError('timer unavailable')
        self.c.transition = fail_schedule
        self.c.start('abc')
        self.c.player.position = 120
        self.c.player.callback('ended', None)
        with self.assertRaisesRegex(RuntimeError, 'timer unavailable'):
            self.pending.pop(0)()
        _, is_current, resume = self.transitions[0]
        self.assertFalse(is_current())
        resume()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 2, 0))
        self.c.toggle()
        self.assertTrue(self.c.player.is_playing)

    def test_next_track_probe_failure_stays_paused_without_scheduling(self):
        transitions = []
        self.c.transition = lambda *args: transitions.append(args)
        self.c.start('abc')
        self.c.player.position = 120
        self.messages.clear()
        def fail_load(*args, **kwargs):
            raise OSError('bad media')
        self.c.player.load = fail_load
        self.c.player.callback('ended', None)
        self.c.player.callback('ended', None)
        with self.assertRaisesRegex(OSError, 'bad media'):
            self.pending.pop(0)()
        self.pending.pop(0)()
        self.assertEqual(transitions, [])
        self.assertEqual(self.messages, [])
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(self.lib.saved[-1], ('abc', 1, 120))

    def test_manual_start_failure_still_cancels_old_transition(self):
        _, is_current, resume = self.prepare_transition()
        def fail_load(*args, **kwargs):
            raise OSError('bad media')
        self.c.player.load = fail_load
        with self.assertRaisesRegex(OSError, 'bad media'):
            self.c.start('other', index=0)
        self.assertFalse(is_current())
        resume()
        self.assertFalse(self.c.player.is_playing)
        self.assertEqual(self.lib.saved[-1], ('abc', 2, 0))

    def test_resume_play_failure_consumes_transition(self):
        _, is_current, resume = self.prepare_transition()
        def fail_play():
            raise OSError('cannot play')
        self.c.player.play = fail_play
        with self.assertRaisesRegex(OSError, 'cannot play'):
            resume()
        self.assertFalse(is_current())
        resume()
        self.assertFalse(self.c.player.is_playing)

    def test_eof_delivered_inside_auto_advance_pause_stays_stale(self):
        self.c.start('abc', index=0)
        original_pause = self.c.player.pause
        def eof_in_pause():
            self.c.player.callback('ended', None)
            original_pause()
        self.c.player.pause = eof_in_pause
        self.c.player.callback('ended', None)
        self.pending.pop(0)()
        self.assertEqual(len(self.pending), 1)
        self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 1)
        self.assertEqual(len(self.c.player.loaded), 2)

    def test_error_is_announced_not_treated_as_end(self):
        self.c.start('abc')
        self.c.player.callback('error', 'awaria')
        self.pending.pop(0)()
        self.assertEqual(self.c.track_index, 1)
        self.assertIn('awaria', self.messages[-1])


if __name__ == '__main__': unittest.main()
