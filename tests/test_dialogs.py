from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

try:
    import wx
except ImportError:
    wx = None
FILE = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/dialogs.py'


class Host:
    def __init__(self):
        self.calls = []
        self.playing = False
        self.auto_next = True
        self.automatic_messages = True
    def get_automatic_messages(self): return self.automatic_messages
    def set_automatic_messages(self, value):
        self.automatic_messages = value
        self.calls.append(("automatic_messages", value))
    def is_playing(self): return self.playing
    def get_auto_next(self): return self.auto_next
    def set_auto_next(self, value):
        self.auto_next = value
        self.calls.append(('auto_next', value))
    def show_sleep_timer(self): self.calls.append(('sleep_timer',))
    def sleep_timer_gesture(self): self.calls.append(('sleep_timer_gesture',))
    def start_book(self, key): self.calls.append(('start', key))
    def action(self, name, *args): self.calls.append((name, *args))
    def adjust(self, name, delta):
        self.calls.append((name, delta))
        return True
    def end_adjustment(self): pass
    def choose_zip(self): self.calls.append(('zip',))
    def choose_folder(self): self.calls.append(('folder',))
    def show_help(self): self.calls.append(('help',))
    def show_output_devices(self): self.calls.append(('output_devices',))
    def remove_book(self, key): self.calls.append(('remove', key))
    def dialog_closed(self, dialog): self.calls.append(('closed',))


@unittest.skipUnless(wx is not None and sys.platform == 'win32', 'Prawdziwe wx wymaga Windows')
class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = wx.App(False)
    def setUp(self):
        self.assertTrue(FILE.exists(), 'Brak okna biblioteki')
        spec = importlib.util.spec_from_file_location('book_dialogs_test', FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.host = Host()
        self.module = module
        self.books = [{'id': 'a', 'title': 'Pierwsza', 'tracks': [{},{}]},
                      {'id': 'b', 'title': 'Druga', 'tracks': [{}]}]
        self.dlg = module.LibraryDialog(None, self.host, self.books)
    def tearDown(self):
        if hasattr(self, 'dlg') and self.dlg:
            self.dlg.stop_adjustment()
            self.dlg.Destroy()
    def test_ctrl_w_opens_outputs_once_without_stealing_child_or_modified_keys(self):
        self.send_key(self.dlg.bookList, ord('W'), ctrl=True)
        self.assertEqual(self.host.calls, [('output_devices',)])
        self.send_key(self.dlg.bookList, ord('W'), ctrl=True, repeat=True)
        for modifiers in ({'ctrl': True, 'shift': True}, {'ctrl': True, 'alt': True},
                          {'ctrl': True, 'meta': True}, {}):
            self.send_key(self.dlg.bookList, ord('W'), **modifiers)
        self.assertEqual(self.host.calls, [('output_devices',)])
        child = wx.TextEntryDialog(self.dlg, 'Tekst')
        try:
            self.send_key(child, ord('W'), ctrl=True)
            self.assertEqual(self.host.calls, [('output_devices',)])
        finally:
            child.Destroy()

    def test_selection_play_remove_and_refresh(self):
        self.assertEqual(self.dlg.bookList.GetCount(), 2)
        self.assertEqual(self.dlg.bookList.GetName(), 'Książki w bibliotece')
        self.dlg.bookList.SetSelection(1)
        self.dlg.on_play(None)
        self.dlg.on_remove(None)
        self.assertEqual(self.host.calls, [('start','b'),('remove','b')])
        self.dlg.refresh(self.books)
        self.assertEqual(self.dlg.bookList.GetSelection(),1)
    def test_select_first_resets_existing_selection_without_starting_playback(self):
        self.dlg.bookList.SetSelection(1)
        self.dlg.select_first()
        self.assertEqual(self.dlg.bookList.GetSelection(), 0)
        self.assertEqual(self.dlg.selected()['id'], 'a')
        self.assertEqual(self.host.calls, [])
        self.dlg.refresh([])
        self.dlg.select_first()
        self.assertEqual(self.dlg.bookList.GetSelection(), wx.NOT_FOUND)
        self.assertIsNone(self.dlg.selected())
        self.assertFalse(self.dlg.playButton.IsEnabled())

    def test_initial_open_selects_first_book_and_focuses_list_without_playback(self):
        for books in (self.books, self.books[:1], []):
            with self.subTest(count=len(books)):
                dialog = self.module.LibraryDialog(None, self.host, books)
                try:
                    dialog.Show()
                    self.app.ProcessPendingEvents()
                    wx.YieldIfNeeded()
                    self.assertEqual(dialog.bookList.GetSelection(), 0 if books else wx.NOT_FOUND)
                    self.assertIs(wx.Window.FindFocus(), dialog.bookList)
                    self.assertEqual(self.host.calls, [])
                finally:
                    dialog.Destroy()

    def test_refresh_keeps_selected_book_across_reorder_without_playback(self):
        self.dlg.bookList.SetSelection(1)
        self.dlg.refresh(list(reversed(self.books)))
        self.assertEqual(self.dlg.selected()['id'], 'b')
        self.assertEqual(self.dlg.bookList.GetSelection(), 0)
        self.dlg.refresh(self.books[:1])
        self.assertEqual(self.dlg.selected()['id'], 'a')
        self.assertEqual(self.host.calls, [])

    def test_empty_library_has_disabled_book_actions(self):
        self.dlg.refresh([])
        self.assertFalse(self.dlg.playButton.IsEnabled())
        self.assertFalse(self.dlg.pauseButton.IsEnabled())
        self.assertTrue(self.dlg.zipButton.IsEnabled())
    def test_empty_library_hides_book_controls_and_restores_them(self):
        buttons = [child for child in self.dlg.panel.GetChildren() if isinstance(child, wx.Button)]
        always_visible = {'Dodaj archiwum', 'Dodaj folder', 'Pomoc (F1)', 'Zamknij (Escape)'}
        book_buttons = [b for b in buttons if b.GetLabel() not in always_visible]
        self.assertEqual(len(book_buttons), 2)
        for books in ([], self.books, []):
            self.dlg.refresh(books)
            for button in buttons:
                with self.subTest(books=bool(books), label=button.GetLabel()):
                    expected = bool(books) or button.GetLabel() in always_visible
                    self.assertEqual(button.IsShown(), expected)
            expected_default = self.dlg.playButton if books else self.dlg.zipButton
            self.assertIs(self.dlg.GetDefaultItem(), expected_default)

    def send_key(self, focus, key, *, ctrl=False, alt=False, shift=False, meta=False, repeat=False):
        event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
        event.SetKeyCode(key)
        event.SetControlDown(ctrl)
        event.SetAltDown(alt)
        event.SetShiftDown(shift)
        event.SetMetaDown(meta)
        with patch.object(self.dlg, 'FindFocus', return_value=focus), \
             patch.object(event, 'IsAutoRepeat', return_value=repeat):
            self.dlg.on_key(event)
        return event

    @contextmanager
    def held_keys(self, *keys):
        # No real key injection: only physical-state reads and time are injected.
        pressed = set(keys)
        timers = []
        class Later:
            def __init__(timer, delay, callback, *args, **kwargs):
                self.assertEqual(delay, 100)
                timer.stopped = False
                timer.callback = lambda: callback(*args, **kwargs)
                timers.append(timer)
            def Stop(timer): timer.stopped = True
        with patch.object(wx, 'GetKeyState', side_effect=lambda key: key in pressed), \
             patch.object(wx, 'CallLater', Later), \
             patch.object(self.dlg, 'FindFocus', return_value=self.dlg.bookList), \
             patch.object(self.dlg, 'IsActive', return_value=True):
            try:
                yield pressed, timers
            finally:
                self.dlg.stop_adjustment()

    def test_held_volume_uses_native_repeat_without_timer_steps(self):
        self.host.playing = True
        for key, delta in ((wx.WXK_UP, 1), (wx.WXK_DOWN, -1)):
            with self.subTest(delta=delta):
                self.host.calls.clear()
                with self.held_keys(key, wx.WXK_SHIFT) as (pressed, timers):
                    self.send_key(self.dlg.bookList, key, shift=True)
                    self.assertEqual(self.host.calls, [('change_volume', delta)])
                    self.assertEqual(len(timers), 1)
                    for _ in range(8):
                        self.send_key(self.dlg.bookList, key, shift=True, repeat=True)
                    self.assertEqual(self.host.calls, [('change_volume', delta)] * 9)
                    self.assertEqual(len(timers), 1)
                    timers[-1].callback()
                    self.assertEqual(len(timers), 2)
                    timers[-1].callback()
                    self.assertEqual(len(timers), 3)
                    self.assertEqual(self.host.calls, [('change_volume', delta)] * 9)

    def test_native_volume_repeat_checks_physical_hold_before_watchdog_tick(self):
        for reason in ('arrow', 'shift', 'ctrl', 'alt', 'meta_left', 'meta_right', 'focus', 'inactive'):
            with self.subTest(reason=reason):
                self.host.playing = True
                self.host.calls.clear()
                with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers), \
                     patch.object(self.host, 'end_adjustment') as end:
                    self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
                    stale = timers[-1]
                    focus = self.dlg.bookList
                    if reason == 'arrow': pressed.remove(wx.WXK_UP)
                    elif reason == 'shift': pressed.remove(wx.WXK_SHIFT)
                    elif reason == 'ctrl': pressed.add(wx.WXK_CONTROL)
                    elif reason == 'alt': pressed.add(wx.WXK_ALT)
                    elif reason == 'meta_left': pressed.add(wx.WXK_WINDOWS_LEFT)
                    elif reason == 'meta_right': pressed.add(wx.WXK_WINDOWS_RIGHT)
                    elif reason == 'focus': focus = self.dlg.status
                    else: self.dlg.IsActive.return_value = False
                    # A queued event may still carry the original modifiers.
                    self.send_key(focus, wx.WXK_UP, shift=True, repeat=True)
                    self.assertEqual(self.host.calls, [('change_volume', 1)])
                    end.assert_called_once_with()
                    self.assertIsNone(self.dlg._adjustment)
                    self.assertTrue(stale.stopped)
                    pressed.clear()
                    pressed.update((wx.WXK_UP, wx.WXK_SHIFT))
                    self.dlg.IsActive.return_value = True
                    self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True, repeat=True)
                    stale.callback()
                    self.assertEqual(self.host.calls, [('change_volume', 1)])
                    self.assertEqual(len(timers), 1)

    def test_polling_stops_on_release_changed_modifiers_or_focus(self):
        for reason in ('arrow', 'shift', 'ctrl', 'alt', 'meta', 'focus', 'inactive'):
            with self.subTest(reason=reason):
                self.host.playing = True
                self.host.calls.clear()
                with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers), \
                     patch.object(self.host, 'end_adjustment') as end:
                    self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
                    stale = timers[-1]
                    if reason == 'arrow': pressed.remove(wx.WXK_UP)
                    elif reason == 'shift': pressed.remove(wx.WXK_SHIFT)
                    elif reason == 'ctrl': pressed.add(wx.WXK_CONTROL)
                    elif reason == 'alt': pressed.add(wx.WXK_ALT)
                    elif reason == 'meta': pressed.add(wx.WXK_WINDOWS_LEFT)
                    elif reason == 'focus': self.dlg.FindFocus.return_value = self.dlg.status
                    else: self.dlg.IsActive.return_value = False
                    stale.callback()
                    self.assertEqual(self.host.calls, [('change_volume', 1)])
                    end.assert_called_once_with()
                    self.assertTrue(stale.stopped)
                    stale.callback()  # Queued callbacks cannot revive a stopped hold.
                    self.assertEqual(len(timers), 1)
                    self.assertEqual(len(self.host.calls), 1)

    def test_release_and_window_events_stop_before_next_tick(self):
        for reason in ('key_up', 'shift_up', 'kill_focus', 'deactivate', 'close', 'destroy'):
            with self.subTest(reason=reason):
                self.host.playing = True
                self.host.calls.clear()
                with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers), \
                     patch.object(self.host, 'end_adjustment') as end:
                    self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
                    stale = timers[-1]
                    target = self.dlg
                    if reason in ('key_up', 'shift_up'):
                        event = wx.KeyEvent(wx.wxEVT_KEY_UP)
                        event.SetKeyCode(wx.WXK_UP if reason == 'key_up' else wx.WXK_SHIFT)
                        target = self.dlg.bookList
                    elif reason == 'kill_focus':
                        event = wx.FocusEvent(wx.wxEVT_KILL_FOCUS, self.dlg.bookList.GetId())
                        target = self.dlg.bookList
                    elif reason == 'deactivate':
                        event = wx.ActivateEvent(wx.wxEVT_ACTIVATE, False)
                    elif reason == 'close':
                        event = wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW)
                    else:
                        event = wx.WindowDestroyEvent(self.dlg)
                        event.SetId(self.dlg.GetId())
                    event.SetEventObject(target)
                    target.GetEventHandler().ProcessEvent(event)
                    end.assert_called_once_with()
                    self.assertTrue(stale.stopped)
                    stale.callback()
                    self.assertEqual(self.host.calls.count(('change_volume', 1)), 1)
                    self.assertEqual(len(timers), 1)


    def send_navigation(self, direction):
        key = wx.WXK_LEFT if direction == 'previous_track' else wx.WXK_RIGHT
        self.send_key(self.dlg.bookList, key, alt=True)

    @contextmanager
    def navigation_host(self, direction, *, boundary=True):
        # Real plugin/controller, fake player and in-memory library: no NVDA,
        # user library, actual audio, or power action is touched.
        import test_adjustment_plugin
        fixture = test_adjustment_plugin.AdjustmentPluginTests()
        fixture.setUp()
        try:
            with patch.dict(sys.modules, {'wx': wx}):
                self.dlg.Destroy()
                self.host = fixture.p
                controller = fixture.core.c
                last = len(controller.library.get_book(controller.book_id)['tracks']) - 1
                index = (0 if direction == 'previous_track' else last) if boundary else (
                    last if direction == 'previous_track' else 0)
                controller.start(controller.book_id, index)
                controller.player.duration = 600
                controller.player.position = 100
                self.dlg = self.module.LibraryDialog(None, self.host, self.books)
                try:
                    yield fixture, controller
                finally:
                    self.dlg.stop_adjustment()
                    self.dlg.Destroy()
        finally:
            fixture.doCleanups()
            fixture.fixture.doCleanups()

    def test_boundary_navigation_keeps_hold_until_key_up(self):
        for direction in ('previous_track', 'next_track'):
            with self.subTest(direction=direction), \
                 self.navigation_host(direction) as (fixture, controller), \
                 self.held_keys(wx.WXK_RIGHT, wx.WXK_CONTROL) as (pressed, timers), \
                 patch.object(self.host, 'end_adjustment', wraps=self.host.end_adjustment) as end, \
                 patch.object(controller.player, 'play', wraps=controller.player.play) as play:
                self.send_key(self.dlg.bookList, wx.WXK_RIGHT, ctrl=True)
                fixture.jobs.pop(0)()
                session = self.host._adjust_session
                held = self.dlg._adjustment
                timer = timers[-1]
                end.reset_mock()  # Starting a fresh host session calls end.
                self.assertFalse(controller.player.is_playing)
                self.send_navigation(direction)
                end.assert_not_called()
                while fixture.jobs:
                    fixture.jobs.pop(0)()
                self.assertIs(self.dlg._adjustment, held)
                self.assertIs(self.host._adjust_session, session)
                self.assertFalse(session.released)
                self.assertFalse(timer.stopped)
                play.assert_not_called()
                event = wx.KeyEvent(wx.wxEVT_KEY_UP)
                event.SetKeyCode(wx.WXK_CONTROL)
                self.dlg.bookList.GetEventHandler().ProcessEvent(event)
                self.dlg.bookList.GetEventHandler().ProcessEvent(event)
                while fixture.jobs:
                    fixture.jobs.pop(0)()
                end.assert_called_once_with()
                play.assert_called_once_with()
                self.assertTrue(controller.player.is_playing)
                self.assertTrue(timer.stopped)
                self.assertIsNone(self.dlg._adjustment)
                timer.callback()
                self.assertEqual(fixture.jobs, [])

    def test_navigation_watchdog_does_not_repeat_old_gesture_during_or_after_worker(self):
        gestures = (
            (wx.WXK_RIGHT, {'ctrl': True}, (wx.WXK_CONTROL,)),
            (wx.WXK_UP, {'ctrl': True, 'shift': True}, (wx.WXK_CONTROL, wx.WXK_SHIFT)),
            (wx.WXK_UP, {'shift': True}, (wx.WXK_SHIFT,)),
        )
        for direction in ('previous_track', 'next_track'):
            for boundary in (True, False):
                for key, mods, physical in gestures:
                    with self.subTest(direction=direction, boundary=boundary, mods=mods), \
                         self.navigation_host(direction, boundary=boundary) as (fixture, controller), \
                         self.held_keys(key, *physical) as (pressed, timers), \
                         patch.object(self.host, 'end_adjustment', wraps=self.host.end_adjustment) as end, \
                         patch.object(self.host, 'adjust', wraps=self.host.adjust) as adjust:
                        self.send_key(self.dlg.bookList, key, **mods)
                        fixture.jobs.pop(0)()
                        held = self.dlg._adjustment
                        session = self.host._adjust_session
                        snapshot = session.snapshot()
                        end.reset_mock()
                        self.send_navigation(direction)
                        self.assertTrue(self.host._busy)
                        # Dispatching a queued command is not a physical key
                        # release; modifier changes are tested separately.
                        timers[-1].callback()
                        self.send_key(self.dlg.bookList, key, repeat=True, **mods)
                        end.assert_not_called()
                        self.assertIs(self.dlg._adjustment, held)
                        self.assertEqual(session.snapshot(), snapshot)
                        while fixture.jobs:
                            fixture.jobs.pop(0)()
                        if boundary:
                            self.assertIs(self.host._adjust_session, session)
                            self.assertFalse(session.released)
                        else:
                            self.assertIsNone(self.host._adjust_session)
                        position = controller.player.position
                        for _ in range(3):
                            timers[-1].callback()
                            self.send_key(self.dlg.bookList, key, repeat=True, **mods)
                        adjust.assert_called_once()
                        end.assert_not_called()
                        self.assertEqual(fixture.jobs, [])
                        self.assertEqual(controller.player.position, position)
                        # Even with no key-up delivered, the physical watchdog
                        # releases exactly once and invalidates queued ticks.
                        pressed.remove(physical[0])
                        stale = timers[-1]
                        stale.callback()
                        stale.callback()
                        end.assert_called_once_with()
                        while fixture.jobs:
                            fixture.jobs.pop(0)()
                        self.assertIsNone(self.dlg._adjustment)
                        self.assertIsNone(self.host._adjust_session)
                        self.assertTrue(controller.player.is_playing)
                        self.send_key(self.dlg.bookList, key, repeat=True, **mods)
                        adjust.assert_called_once()
                        # A new deliberate press still starts a new gesture.
                        pressed.add(physical[0])
                        self.send_key(self.dlg.bookList, key, **mods)
                        self.assertEqual(adjust.call_count, 2)

    def test_boundary_navigation_accepts_real_release_while_worker_is_pending(self):
        for release in ('key_up', 'changed_modifiers'):
            with self.subTest(release=release), \
                 self.navigation_host('previous_track') as (fixture, controller), \
                 self.held_keys(wx.WXK_RIGHT, wx.WXK_CONTROL) as (pressed, timers), \
                 patch.object(self.host, 'end_adjustment', wraps=self.host.end_adjustment) as end, \
                 patch.object(controller.player, 'play', wraps=controller.player.play) as play:
                self.send_key(self.dlg.bookList, wx.WXK_RIGHT, ctrl=True)
                fixture.jobs.pop(0)()
                session = self.host._adjust_session
                end.reset_mock()
                self.send_navigation('previous_track')
                self.assertTrue(self.host._busy)
                if release == 'key_up':
                    event = wx.KeyEvent(wx.wxEVT_KEY_UP)
                    event.SetKeyCode(wx.WXK_RIGHT)
                    self.dlg.bookList.GetEventHandler().ProcessEvent(event)
                else:
                    # Unlike the synthetic navigation event, this really
                    # changes the physical modifier state seen by wx.
                    pressed.remove(wx.WXK_CONTROL)
                    pressed.add(wx.WXK_ALT)
                    timers[-1].callback()
                end.assert_called_once_with()
                self.assertTrue(session.released)
                self.assertIsNone(self.dlg._adjustment)
                play.assert_not_called()
                while fixture.jobs:
                    fixture.jobs.pop(0)()
                play.assert_called_once_with()
                self.assertTrue(controller.player.is_playing)
                timers[-1].callback()
                end.assert_called_once_with()
                self.assertEqual(fixture.jobs, [])

    def test_commands_finish_hold_except_navigation_release_tracking(self):
        commands = [
            (ord('P'), {'ctrl': True}), (ord('I'), {'ctrl': True}),
            (ord('I'), {'ctrl': True, 'shift': True}),
            (ord('T'), {'shift': True}), (ord('K'), {'shift': True}), (wx.WXK_SPACE, {}),
            (ord('K'), {'ctrl': True, 'shift': True}),
            (wx.WXK_RETURN, {}), (wx.WXK_DELETE, {}), (wx.WXK_ESCAPE, {}),
            (wx.WXK_LEFT, {'alt': True}), (wx.WXK_RIGHT, {'alt': True}),
        ]
        buttons = [c for c in self.dlg.panel.GetChildren() if isinstance(c, wx.Button)]
        for command in commands + buttons:
            with self.subTest(command=command):
                self.host.playing = True
                self.host.calls.clear()
                with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers), \
                     patch.object(self.host, 'end_adjustment',
                                  side_effect=lambda: self.host.calls.append(('end',))):
                    self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
                    stale = timers[-1]
                    if isinstance(command, tuple):
                        self.send_key(self.dlg.bookList, command[0], **command[1])
                    else:
                        event = wx.CommandEvent(wx.wxEVT_BUTTON, command.GetId())
                        event.SetEventObject(command)
                        command.GetEventHandler().ProcessEvent(event)
                    # Mutating playback commands finish the host transaction
                    # atomically; preliminary release would permit a restart.
                    mutates = self.host.calls[-1][0] in ('toggle', 'toggle_selected', 'start', 'previous_track', 'next_track')
                    expected_count = 2 if mutates else 3
                    if mutates:
                        self.assertNotIn(('end',), self.host.calls)
                    else:
                        self.assertEqual(self.host.calls[1], ('end',))
                    self.assertEqual(len(self.host.calls), expected_count)
                    navigation = self.host.calls[-1][0] in ('previous_track', 'next_track')
                    if navigation:
                        self.assertIsNotNone(self.dlg._adjustment)
                        self.assertFalse(stale.stopped)
                    else:
                        self.assertIsNone(self.dlg._adjustment)
                        self.assertTrue(stale.stopped)
                    stale.callback()
                    self.assertEqual(len(self.host.calls), expected_count)

    def test_seek_uses_held_adjustment_from_library_controls_only(self):
        timer = self.module.SleepTimerDialog(self.dlg)
        self.addCleanup(timer.Destroy)
        for key, delta in ((wx.WXK_LEFT, -5), (wx.WXK_RIGHT, 5)):
            for focus in (self.dlg.bookList, self.dlg.status, self.dlg.playButton):
                with self.subTest(key=key, focus=focus.GetName()):
                    self.host.calls.clear()
                    with self.held_keys(key, wx.WXK_CONTROL) as (pressed, timers), \
                         patch.object(self.host, 'adjust', wraps=self.host.adjust) as adjust, \
                         patch.object(self.host, 'action', wraps=self.host.action) as action:
                        self.dlg.FindFocus.return_value = focus
                        self.assertFalse(self.send_key(focus, key, ctrl=True).GetSkipped())
                        adjust.assert_called_once_with('seek_relative', delta)
                        action.assert_not_called()
                        for _ in range(4): self.send_key(focus, key, ctrl=True)
                        self.assertEqual(adjust.call_count, 1)
                        timers[-1].callback()
                        self.assertEqual(self.host.calls, [('seek_relative', delta)] * 2)
            for focus in (timer.when, timer.action, timer.count, timer.applyButton):
                with self.subTest(key=key, focus=focus.GetName()):
                    self.host.calls.clear()
                    self.assertTrue(self.send_key(focus, key, ctrl=True).GetSkipped())
                    self.assertEqual(self.host.calls, [])

    def test_host_rejection_stops_initial_or_repeating_adjustment(self):
        for reject_first in (True, False):
            with self.subTest(reject_first=reject_first):
                self.host.playing = True
                with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers), \
                     patch.object(self.host, 'adjust', side_effect=[False] if reject_first else [True, False]) as adjust, \
                     patch.object(self.host, 'end_adjustment') as end:
                    self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
                    if reject_first:
                        self.assertEqual(timers, [])
                    else:
                        stale = timers[-1]
                        self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True, repeat=True)
                        self.assertTrue(stale.stopped)
                        stale.callback()
                        self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True, repeat=True)
                        self.assertEqual(len(timers), 1)
                    self.assertEqual(adjust.call_count, 1 if reject_first else 2)
                    end.assert_called_once_with()

    def test_os_repeat_cannot_restart_stopped_hold(self):
        self.host.playing = True
        with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers):
            self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
            self.dlg.stop_adjustment()
            for _ in range(5):
                self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True, repeat=True)
            self.assertEqual(self.host.calls, [('change_volume', 1)])
            self.assertEqual(len(timers), 1)
            self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
            self.assertEqual(self.host.calls, [('change_volume', 1)] * 2)

    def test_double_click_ends_hold_before_starting_book(self):
        self.host.playing = True
        with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers), \
             patch.object(self.host, 'end_adjustment',
                          side_effect=lambda: self.host.calls.append(('end',))):
            self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
            event = wx.CommandEvent(wx.wxEVT_LISTBOX_DCLICK, self.dlg.bookList.GetId())
            event.SetEventObject(self.dlg.bookList)
            self.dlg.bookList.GetEventHandler().ProcessEvent(event)
            self.assertEqual(self.host.calls, [('change_volume', 1), ('start', 'a')])
            self.assertIsNone(self.dlg._adjustment)
            timers[-1].callback()
            self.assertEqual(len(self.host.calls), 2)

    def test_host_can_end_adjustment_during_dispatch_without_rearming(self):
        self.host.playing = True
        for first in (True, False):
            with self.subTest(first=first):
                with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers):
                    def adjust(name, delta):
                        self.dlg.stop_adjustment()
                        return True
                    if not first:
                        self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
                    with patch.object(self.host, 'adjust', side_effect=adjust):
                        if first:
                            self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
                        else:
                            self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True, repeat=True)
                    self.assertEqual(len(timers), 0 if first else 1)
                    self.assertIsNone(self.dlg._adjustment)
                    if timers:
                        self.assertTrue(timers[-1].stopped)
                        timers[-1].callback()
                    self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True, repeat=True)
                    self.assertEqual(len(timers), 0 if first else 1)

    def test_all_held_steps_continue_during_backend_pause_and_stop_on_release(self):
        for key, mods, physical, action, delta in (
            (wx.WXK_UP, {'shift': True}, (wx.WXK_SHIFT,), 'change_volume', 1),
            (wx.WXK_DOWN, {'shift': True}, (wx.WXK_SHIFT,), 'change_volume', -1),
            (wx.WXK_UP, {'ctrl': True, 'shift': True},
             (wx.WXK_CONTROL, wx.WXK_SHIFT), 'change_speed', 0.5),
            (wx.WXK_DOWN, {'ctrl': True, 'shift': True},
             (wx.WXK_CONTROL, wx.WXK_SHIFT), 'change_speed', -0.5),
            (wx.WXK_LEFT, {'ctrl': True}, (wx.WXK_CONTROL,), 'seek_relative', -5),
            (wx.WXK_RIGHT, {'ctrl': True}, (wx.WXK_CONTROL,), 'seek_relative', 5),
        ):
            with self.subTest(action=action, delta=delta):
                self.host.playing = True
                self.host.calls.clear()
                with self.held_keys(key, *physical) as (pressed, timers), \
                     patch.object(self.host, 'end_adjustment') as end:
                    self.send_key(self.dlg.bookList, key, **mods)
                    self.host.playing = False  # ffplay may be restarting after a step.
                    for _ in range(4):
                        self.assertFalse(self.send_key(self.dlg.bookList, key, repeat=True, **mods).GetSkipped())
                    native_count = 5 if action == 'change_volume' else 1
                    self.assertEqual(self.host.calls, [(action, delta)] * native_count)
                    self.assertEqual(len(timers), 1)
                    timers[-1].callback()
                    timers[-1].callback()
                    expected_count = 5 if action == 'change_volume' else 3
                    self.assertEqual(self.host.calls, [(action, delta)] * expected_count)
                    self.assertEqual(len(timers), 3)
                    pressed.remove(physical[0])
                    timers[-1].callback()
                    end.assert_called_once_with()
                    self.assertEqual(len(self.host.calls), expected_count)

    def test_library_has_no_playback_menu(self):
        self.assertIsNone(self.dlg.GetMenuBar())

    def test_stale_tick_after_real_window_destruction_is_harmless(self):
        self.host.playing = True
        with self.held_keys(wx.WXK_UP, wx.WXK_SHIFT) as (pressed, timers), \
             patch.object(self.host, 'end_adjustment') as end:
            self.send_key(self.dlg.bookList, wx.WXK_UP, shift=True)
            self.dlg.Destroy()
            self.app.ProcessPendingEvents()
            wx.YieldIfNeeded()
            self.assertFalse(bool(self.dlg))
            end.assert_called_once_with()
            timers[-1].callback()
            self.assertEqual(self.host.calls, [('change_volume', 1)])

    def test_playing_arrows_change_volume_or_speed_only_on_book_list(self):
        self.host.playing = True
        for control in (self.dlg.bookList,):
            for ctrl, action, step in ((False, 'change_volume', 1),
                                       (True, 'change_speed', 0.5)):
                for key, sign in ((wx.WXK_UP, 1), (wx.WXK_DOWN, -1)):
                    with self.subTest(control=control.GetName(), key=key, ctrl=ctrl):
                        self.host.calls.clear()
                        event = self.send_key(control, key, ctrl=ctrl, shift=True)
                        self.assertEqual(self.host.calls, [(action, step * sign)])
                        self.assertFalse(event.GetSkipped())

    def test_plain_arrows_always_preserve_native_navigation(self):
        for playing in (False, True):
            self.host.playing = playing
            for key in (wx.WXK_UP, wx.WXK_DOWN):
                self.assertTrue(self.send_key(self.dlg.bookList, key).GetSkipped())
        self.assertEqual(self.host.calls, [])

    def test_paused_arrows_preserve_native_navigation(self):
        for control in (self.dlg.bookList, self.dlg.status):
            for key in (wx.WXK_UP, wx.WXK_DOWN):
                for shift in (False, True):
                    event = self.send_key(control, key, shift=shift)
                    self.assertTrue(event.GetSkipped())
        self.assertEqual(self.host.calls, [])

    def test_playing_arrows_leave_editable_fields_buttons_and_modifiers_alone(self):
        self.host.playing = True
        editable = wx.TextCtrl(self.dlg)
        try:
            for control in (editable, self.dlg.status, self.dlg.playButton, self.dlg.zipButton):
                for key in (wx.WXK_UP, wx.WXK_DOWN):
                    for shift in (False, True):
                        self.assertTrue(self.send_key(control, key, shift=shift).GetSkipped())
            for key in (wx.WXK_UP, wx.WXK_DOWN):
                for mods in ({'ctrl': True}, {'alt': True}, {'alt': True, 'shift': True},
                             {'meta': True}, {'meta': True, 'shift': True}):
                    self.assertTrue(self.send_key(self.dlg.bookList, key, **mods).GetSkipped())
            self.assertEqual(self.host.calls, [])
        finally:
            editable.Destroy()

    def test_existing_keyboard_shortcuts_are_preserved(self):
        for key, mods, expected in (
            (ord('P'), {'ctrl': True}, ('toggle_selected',)),
            (ord('I'), {'ctrl': True}, ('status',)),
            (ord('I'), {'ctrl': True, 'shift': True}, ('book_status',)),
            (wx.WXK_LEFT, {'ctrl': True}, ('seek_relative', -5)),
            (wx.WXK_RIGHT, {'ctrl': True}, ('seek_relative', 5)),
            (wx.WXK_LEFT, {'alt': True}, ('previous_track',)),
            (wx.WXK_RIGHT, {'alt': True}, ('next_track',)),
            (wx.WXK_RETURN, {}, ('toggle_selected',)),
            (wx.WXK_DELETE, {}, ('remove', 'a')),
            (wx.WXK_SPACE, {}, ('toggle_selected',)),
        ):
            self.host.calls.clear()
            self.assertFalse(self.send_key(self.dlg.bookList, key, **mods).GetSkipped())
            self.assertEqual(self.host.calls, [expected])

    def test_book_status_shortcut_dispatches_with_or_without_library_selection(self):
        for books in (self.books, []):
            self.host.calls.clear()
            self.dlg.refresh(books)
            self.send_key(self.dlg.bookList, ord('I'), ctrl=True, shift=True)
            self.assertEqual(self.host.calls, [('book_status',)])

    def test_information_shortcuts_reject_extra_modifiers(self):
        for mask in range(16):
            if mask in (1, 5):
                continue
            mods = dict(zip(('ctrl', 'alt', 'shift', 'meta'),
                            (bool(mask & bit) for bit in (1, 2, 4, 8))))
            self.assertTrue(self.send_key(self.dlg.bookList, ord('I'), **mods).GetSkipped())
        self.assertEqual(self.host.calls, [])

    def test_plain_t_and_k_preserve_list_type_to_search(self):
        for playing in (False, True):
            self.host.playing = playing
            for key in (ord('T'), ord('K')):
                for repeat in (False, True):
                    self.assertTrue(self.send_key(self.dlg.bookList, key, repeat=repeat).GetSkipped())
        self.assertEqual(self.host.calls, [])

    def test_shift_t_and_k_ignore_autorepeat_but_accept_separate_presses(self):
        for playing in (False, True):
            for key in (ord('T'), ord('K')):
                with self.subTest(playing=playing, key=key):
                    self.host.playing = playing
                    self.host.auto_next = True
                    self.host.calls.clear()
                    self.send_key(self.dlg.bookList, key, shift=True)
                    for _ in range(5):
                        self.assertFalse(self.send_key(self.dlg.bookList, key, shift=True, repeat=True).GetSkipped())
                    expected = [('sleep_timer_gesture',)] if key == ord('T') else [('auto_next', False)]
                    self.assertEqual(self.host.calls, expected)
                    self.send_key(self.dlg.bookList, key, shift=True)
                    expected += [('sleep_timer_gesture',)] if key == ord('T') else [('auto_next', True)]
                    self.assertEqual(self.host.calls, expected)

    def test_playing_shift_t_dispatches_gesture_only_on_book_list(self):
        self.host.playing = True
        event = self.send_key(self.dlg.bookList, ord('T'), shift=True)
        self.assertFalse(event.GetSkipped())
        self.assertEqual(self.host.calls, [('sleep_timer_gesture',)])

    def test_playing_shift_k_toggles_current_host_setting_without_checkbox(self):
        self.assertFalse(any(isinstance(c, wx.CheckBox) for c in self.dlg.panel.GetChildren()))
        self.host.playing = True
        for current in (True, False):
            self.host.auto_next = current
            self.host.calls.clear()
            event = self.send_key(self.dlg.bookList, ord('K'), shift=True)
            self.assertFalse(event.GetSkipped())
            self.assertEqual(self.host.calls, [('auto_next', not current)])
            self.assertEqual(self.host.get_auto_next(), not current)
        self.assertFalse(hasattr(self.dlg, 'refresh_auto_next'))

    def test_ctrl_shift_k_toggles_messages_once_per_press_playing_or_paused(self):
        for playing in (False, True):
            with self.subTest(playing=playing):
                self.host.playing = playing
                self.host.automatic_messages = True
                self.host.calls.clear()
                self.assertFalse(self.send_key(self.dlg.bookList, ord('K'),
                                               ctrl=True, shift=True).GetSkipped())
                for _ in range(5):
                    self.assertFalse(self.send_key(self.dlg.bookList, ord('K'),
                                                   ctrl=True, shift=True, repeat=True).GetSkipped())
                self.assertEqual(self.host.calls, [('automatic_messages', False)])
                self.assertFalse(self.host.get_automatic_messages())
                self.send_key(self.dlg.bookList, ord('K'), ctrl=True, shift=True)
                self.assertEqual(self.host.calls, [('automatic_messages', False),
                                                  ('automatic_messages', True)])
                self.assertIs(self.host.playing, playing)
                self.assertTrue(self.host.auto_next)

    def test_ctrl_shift_k_reports_read_or_save_failure_without_changing_setting(self):
        for method in ('get_automatic_messages', 'set_automatic_messages'):
            for error_type in (OSError, ValueError):
                with self.subTest(method=method, error_type=error_type):
                    with patch.object(self.host, method,
                                      side_effect=error_type('Brak dostępu do ustawień.')), \
                         patch.object(wx, 'MessageBox') as error:
                        event = self.send_key(self.dlg.bookList, ord('K'), ctrl=True, shift=True)
                        self.assertFalse(event.GetSkipped())
                        error.assert_called_once_with('Brak dostępu do ustawień.', 'Czytelnia',
                                                      wx.OK | wx.ICON_ERROR, parent=self.dlg)
                    self.assertTrue(self.host.get_automatic_messages())
                    self.assertEqual(self.host.calls, [])

    def test_ctrl_shift_k_works_in_empty_library_without_starting_playback(self):
        self.dlg.refresh([])
        self.assertFalse(self.send_key(self.dlg.bookList, ord('K'),
                                       ctrl=True, shift=True).GetSkipped())
        self.assertEqual(self.host.calls, [('automatic_messages', False)])
        self.assertFalse(self.host.playing)

    def test_k_reports_save_failure_without_changing_setting(self):
        self.host.playing = True
        for error_type in (OSError, ValueError):
            with self.subTest(error_type=error_type):
                with patch.object(self.host, 'set_auto_next',
                                  side_effect=error_type('Brak dostępu do ustawień.')) as setter, \
                     patch.object(wx, 'MessageBox') as error:
                    event = self.send_key(self.dlg.bookList, ord('K'), shift=True)
                    self.assertFalse(event.GetSkipped())
                    setter.assert_called_once_with(False)
                    error.assert_called_once_with('Brak dostępu do ustawień.', 'Czytelnia',
                                                  wx.OK | wx.ICON_ERROR, parent=self.dlg)
                self.assertTrue(self.host.get_auto_next())
                self.assertEqual(self.host.calls, [])

    def test_timer_shortcut_works_even_with_empty_library_and_without_playback(self):
        self.dlg.refresh([])
        self.send_key(self.dlg.bookList, ord('T'), shift=True)
        self.assertEqual(self.host.calls, [('sleep_timer_gesture',)])

    def test_paused_t_opens_timer_and_k_toggles_without_resuming(self):
        self.assertFalse(self.host.playing)
        self.assertFalse(self.send_key(self.dlg.bookList, ord('T'), shift=True).GetSkipped())
        self.assertEqual(self.host.calls, [('sleep_timer_gesture',)])
        for current in (True, False):
            self.host.auto_next = current
            self.host.calls.clear()
            self.assertFalse(self.send_key(self.dlg.bookList, ord('K'), shift=True).GetSkipped())
            self.assertEqual(self.host.auto_next, not current)
            self.assertFalse(self.host.playing)
        self.assertNotIn(('toggle',), self.host.calls)

    def test_t_and_k_leave_extra_modifiers_and_non_list_controls_alone(self):
        editable = wx.TextCtrl(self.dlg)
        timer = self.make_timer()
        try:
            for playing in (False, True):
                self.host.playing = playing
                for key in (ord('T'), ord('K')):
                    for control in (editable, self.dlg.status, self.dlg.playButton,
                                    self.dlg.zipButton,
                                    timer.when, timer.action, timer.count, timer.applyButton):
                        with self.subTest(playing=playing, key=key, control=control.GetName()):
                            for shift in (False, True):
                                for ctrl in (False, True):
                                    self.assertTrue(self.send_key(control, key, ctrl=ctrl, shift=shift).GetSkipped())
                    for mask in range(16):
                        if mask == 4 or (key == ord("K") and mask == 5):  # Shift / Ctrl+Shift+K.
                            continue
                        mods = dict(zip(('ctrl', 'alt', 'shift', 'meta'),
                                        (bool(mask & bit) for bit in (1, 2, 4, 8))))
                        with self.subTest(playing=playing, key=key, modifiers=mods):
                            self.assertTrue(self.send_key(self.dlg.bookList, key, **mods).GetSkipped())
            self.assertEqual(self.host.calls, [])
        finally:
            editable.Destroy()

    def test_timer_modal_does_not_trigger_library_t_or_k(self):
        self.host.playing = True
        timer = self.module.SleepTimerDialog(self.dlg)
        self.addCleanup(timer.Destroy)
        self.select_timer_when(timer, 1)
        visited = []
        def interact():
            try:
                for control in (timer.when, timer.action, timer.count, timer.applyButton):
                    control.SetFocus()
                    focus = wx.Window.FindFocus()
                    visited.append(focus)
                    for key in (ord('T'), ord('K')):
                        event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
                        event.SetKeyCode(key)
                        event.SetShiftDown(True)
                        event.SetEventObject(focus)
                        focus.GetEventHandler().ProcessEvent(event)
            finally:
                timer.EndModal(wx.ID_CANCEL)
        guard = wx.CallLater(2000, lambda: timer.EndModal(wx.ID_ABORT))
        wx.CallAfter(interact)
        try:
            self.assertEqual(timer.ShowModal(), wx.ID_CANCEL)
        finally:
            guard.Stop()
        self.assertEqual(visited, [timer.when, timer.action, timer.count, timer.applyButton])
        self.assertEqual(self.host.calls, [])

    def make_timer(self, state=None):
        self.assertTrue(hasattr(self.module, 'SleepTimerDialog'), 'Brak okna wyłącznika czasowego')
        timer = self.module.SleepTimerDialog(None, state=state)
        self.addCleanup(timer.Destroy)
        return timer

    def select_timer_when(self, timer, index):
        timer.when.SetSelection(index)
        event = wx.CommandEvent(wx.wxEVT_CHOICE, timer.when.GetId())
        event.SetEventObject(timer.when)
        timer.when.GetEventHandler().ProcessEvent(event)

    def test_sleep_timer_native_controls_presets_and_config(self):
        timer = self.make_timer()
        self.assertEqual(timer.GetTitle(), 'Wyłącznik czasowy')
        self.assertIsInstance(timer.when, wx.Choice)
        self.assertNotIsInstance(timer.when, wx.ComboBox)
        self.assertEqual(timer.when.GetName(), 'Kiedy')
        self.assertEqual(timer.when.GetStrings(),
                         ['Po zakończeniu bieżącego odcinka',
                          'Po określonej liczbie odcinków'] +
                         [f'Za {minutes} minut' for minutes in range(15, 121, 15)])
        self.assertIsInstance(timer.action, wx.Choice)
        self.assertEqual(timer.action.GetName(), 'Działanie')
        self.assertEqual(timer.action.GetStrings(),
                         ['Zatrzymaj książkę', 'Przełącz komputer w stan uśpienia', 'Wyłącz komputer'])
        labels = [c.GetLabel() for c in timer.GetChildren() if isinstance(c, wx.StaticText)]
        self.assertIn('Kiedy:', labels)
        self.assertIn('Działanie:', labels)
        self.assertEqual(timer.config(), {'minutes': None, 'action': 'stop'})
        for index, minutes in enumerate(range(15, 121, 15), 2):
            self.select_timer_when(timer, index)
            for action_index, action in enumerate(('stop', 'sleep', 'shutdown')):
                timer.action.SetSelection(action_index)
                self.assertEqual(timer.config(), {'minutes': minutes, 'action': action})
        self.select_timer_when(timer, 0)
        self.assertEqual(timer.config(), {'minutes': None, 'action': 'shutdown'})
        self.assertEqual(timer.FindWindow(wx.ID_OK).GetLabel(), 'Zapisz')
        self.assertEqual(timer.FindWindow(wx.ID_CANCEL).GetLabel(), 'Anuluj')
        self.assertEqual(timer.GetEscapeId(), wx.ID_CANCEL)
        self.assertEqual(timer.GetDefaultItem().GetId(), wx.ID_OK)

    def test_sleep_timer_count_is_labelled_and_only_shown_and_enabled_for_count_mode(self):
        timer = self.make_timer()
        self.assertIsInstance(timer.count, wx.TextCtrl)
        self.assertEqual(timer.count.GetName(), 'Liczba odcinków')
        self.assertEqual(timer.count.GetValue(), '')
        self.assertEqual(timer.count.GetPrevSibling().GetLabel(), 'Liczba odcinków:')
        for index in (0, 1, 2, 1, 9, 0):
            self.select_timer_when(timer, index)
            with self.subTest(index=index):
                self.assertEqual(timer.count.IsShown(), index == 1)
                self.assertEqual(timer.count.IsEnabled(), index == 1)
                self.assertEqual(timer.count.GetPrevSibling().IsShown(), index == 1)
        self.select_timer_when(timer, 1)
        for value in ('1', '2', '10000', ' 27 '):
            timer.count.SetValue(value)
            for index, action in enumerate(('stop', 'sleep', 'shutdown')):
                timer.action.SetSelection(index)
                self.assertEqual(timer.config(), {'minutes': None, 'tracks': int(value), 'action': action})

    def test_sleep_timer_rejects_invalid_count_and_missing_choices(self):
        timer = self.make_timer()
        self.select_timer_when(timer, 1)
        for value in ('', '   '):
            with self.subTest(value=value):
                timer.count.SetValue(value)
                with self.assertRaisesRegex(ValueError, r'^Wpisz liczbę odcinków\.$'):
                    timer.config()
        for value in ('abc', '2 odcinki', '0', '-1', '10001', '1.5', '1,5', '+2',
                      '1_0', '²', '１２', '9' * 5000):
            with self.subTest(value=value[:40]):
                timer.count.SetValue(value)
                with self.assertRaisesRegex(ValueError, '10000'):
                    timer.config()
        # Hidden invalid count cannot prevent selecting another timer mode.
        for index in (0, 2, 9):
            self.select_timer_when(timer, index)
            self.assertNotIn('tracks', timer.config())
        timer.when.SetSelection(wx.NOT_FOUND)
        with self.assertRaises(ValueError):
            timer.config()
        self.select_timer_when(timer, 0)
        timer.action.SetSelection(wx.NOT_FOUND)
        with self.assertRaises(ValueError):
            timer.config()

    def test_sleep_timer_validates_before_accepting_and_reports_polish_error(self):
        timer = self.make_timer()
        button = timer.FindWindow(wx.ID_OK)
        def apply():
            event = wx.CommandEvent(wx.wxEVT_BUTTON, wx.ID_OK)
            event.SetEventObject(button)
            button.GetEventHandler().ProcessEvent(event)
        with patch.object(timer, 'EndModal') as end, patch.object(wx, 'MessageBox') as error, \
             patch.object(timer.count, 'SetFocus') as focus:
            self.select_timer_when(timer, 1)
            timer.count.SetValue('0')
            apply()
            end.assert_not_called()
            error.assert_called_once()
            self.assertIn('1', error.call_args.args[0])
            self.assertIn('10000', error.call_args.args[0])
            self.assertIn('odcinków', error.call_args.args[0])
            self.assertIs(error.call_args.kwargs['parent'], timer)
            focus.assert_called_once_with()
            error.reset_mock()
            timer.count.SetValue('3')
            apply()
            error.assert_not_called()
            end.assert_called_once_with(wx.ID_OK)

    def test_sleep_timer_configuration_has_no_status_or_cancel_timer_ui(self):
        timer = self.make_timer()
        self.assertFalse(hasattr(self.module, 'CANCEL_TIMER_ID'))
        self.assertFalse(hasattr(timer, 'stateInfo'))
        self.assertFalse(hasattr(timer, 'cancelTimerButton'))
        self.assertEqual([c.GetLabel() for c in timer.GetChildren() if isinstance(c, wx.Button)],
                         ['Zapisz', 'Anuluj'])
        self.assertNotIn('Stan wyłącznika:', [c.GetLabel() for c in timer.GetChildren()
                                            if isinstance(c, wx.StaticText)])
        self.select_timer_when(timer, 4)
        fresh = self.make_timer()
        self.assertEqual(fresh.config(), {'minutes': None, 'action': 'stop'})

    def test_sleep_timer_legacy_state_does_not_restore_active_minutes_ui(self):
        state = {'mode': 'minutes', 'action': 'sleep', 'remaining': 125}
        timer = self.make_timer(state)
        self.assertEqual(timer.FindWindow(wx.ID_OK).GetLabel(), 'Zapisz')
        self.assertEqual(timer.config(), {'minutes': None, 'action': 'stop'})
        self.assertFalse(hasattr(timer, 'stateInfo'))
        self.assertEqual(state, {'mode': 'minutes', 'action': 'sleep', 'remaining': 125})

    def test_sleep_timer_legacy_end_state_does_not_restore_active_ui(self):
        timer = self.make_timer({'mode': 'end', 'action': 'shutdown', 'remaining': None})
        self.assertEqual(timer.config(), {'minutes': None, 'action': 'stop'})
        self.assertEqual(timer.FindWindow(wx.ID_OK).GetLabel(), 'Zapisz')
        self.assertFalse(hasattr(timer, 'cancelTimerButton'))

    def test_sleep_timer_native_modal_results_and_tab_order(self):
        for index in range(10):
            for result_id in (wx.ID_OK, wx.ID_CANCEL):
                with self.subTest(index=index, result_id=int(result_id)):
                    timer = self.make_timer()
                    self.select_timer_when(timer, index)
                    if index == 1 and result_id == wx.ID_OK:
                        timer.count.SetValue('2')
                    expected = [timer.when] + ([timer.count] if index == 1 else []) + [
                        timer.action, timer.applyButton, timer.FindWindow(wx.ID_CANCEL)]
                    visited = []
                    def interact():
                        timer.when.SetFocus()
                        for _ in expected:
                            focus = wx.Window.FindFocus()
                            visited.append(focus)
                            focus.Navigate()
                        button = timer.FindWindow(result_id)
                        event = wx.CommandEvent(wx.wxEVT_BUTTON, button.GetId())
                        event.SetEventObject(button)
                        button.GetEventHandler().ProcessEvent(event)
                    guard = wx.CallLater(2000, lambda: timer.EndModal(wx.ID_ABORT))
                    wx.CallAfter(interact)
                    try:
                        self.assertEqual(timer.ShowModal(), result_id)
                    finally:
                        guard.Stop()
                    self.assertEqual(visited, expected)

    def test_candidate_list_preserves_full_paths(self):
        paths = [Path('D:/Książki/Tom 2.zip'),Path('D:/Książki/Tom 10')]
        dlg = self.module.CandidateDialog(None, paths, source=Path('D:/Książki'))
        try:
            self.assertEqual(dlg.sourceCtrl.GetValue(), str(Path('D:/Książki')))
            self.assertFalse(dlg.sourceCtrl.IsEditable())
            self.assertEqual(dlg.candidates.GetCount(),2)
            dlg.candidates.SetSelection(1)
            self.assertEqual(dlg.selected_path(),paths[1])
        finally: dlg.Destroy()


if __name__ == '__main__': unittest.main()
