"""Isolated NVDA API contract tests; never imports or modifies running NVDA."""
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / 'addon/globalPlugins/czytelnia/__init__.py'


class Timer:
    def __init__(self, delay, callback, *args):
        self.delay, self.callback, self.args = delay, callback, args
        self.stopped = False
    def Stop(self): self.stopped = True
    def fire(self):
        if not self.stopped: self.callback(*self.args)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(FILE.exists(), 'Brak globalnego pluginu')
        # Never allow plugin initialization to address the user's real media.
        import os
        self.environment = patch.dict(os.environ, {
            'LOCALAPPDATA': str(ROOT / '.test-work' / 'isolated-localappdata')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.timers = []
        self.repeat = 0
        self.menu = Mock()
        self.menu.Append.return_value = Mock()
        self.modules = {}
        def module(name, **attrs):
            obj = types.ModuleType(name)
            obj.__dict__.update(attrs)
            self.modules[name] = obj
            return obj
        module('globalPluginHandler', GlobalPlugin=type('Base', (), {'terminate':lambda s:None}))
        module('globalVars', appArgs=types.SimpleNamespace(secure=False, configPath=str(ROOT / '.test-work' / 'nvda-config')))
        module('config', conf={'keyboard':{'multiPressTimeout':500}})
        module('scriptHandler', getLastScriptRepeatCount=lambda:self.repeat,
               script=lambda **kwargs: lambda fn:fn)
        module('core', callLater=self.later)
        module('wx', ID_ANY=-1, EVT_MENU=1, CallAfter=lambda fn,*a,**kw:fn(*a,**kw))
        module('gui', mainFrame=types.SimpleNamespace(sysTrayIcon=Mock(toolsMenu=self.menu)))
        module('ui', message=Mock())
        module('speech', cancelSpeech=Mock())
        module('logHandler', log=Mock())
        for name in ('library','player','controller','dialogs','explorer','preferences','settings_panel','sleep_timer','power'):
            module('plugin_test.'+name)
        self.modules['plugin_test.library'].Library = Mock()
        self.modules['plugin_test.library'].FolderSelectionRequired = type('FolderSelectionRequired', (Exception,), {})
        self.modules['plugin_test.library'].scan_candidates = Mock()
        self.modules['plugin_test.player'].Player = Mock()
        self.modules['plugin_test.player'].probe_duration = Mock(return_value=12.5)
        self.modules['plugin_test.controller'].Controller = Mock()
        self.modules['plugin_test.dialogs'].LibraryDialog = Mock()
        self.modules['plugin_test.dialogs'].CandidateDialog = Mock()
        self.modules['plugin_test.dialogs'].SleepTimerDialog = Mock()
        self.modules['plugin_test.dialogs'].ImportProgressDialog = Mock()
        self.modules['plugin_test.dialogs'].CANCEL_TIMER_ID = 999
        self.modules['plugin_test.preferences'].Preferences = Mock(return_value=Mock(auto_next=True))
        self.modules['plugin_test.settings_panel'].register = Mock()
        self.modules['plugin_test.settings_panel'].unregister = Mock()
        self.modules['plugin_test.sleep_timer'].SleepTimer = Mock()
        self.modules['plugin_test.power'].perform = Mock()
        self.modules['plugin_test.explorer'].capture_context = Mock(return_value='context')
        self.modules['plugin_test.explorer'].get_current_folder = Mock(return_value=ROOT)
        self.modules['plugin_test.explorer'].get_selected_zip = Mock(return_value=None)
        self.modules['plugin_test.explorer'].get_selected_source = self.modules['plugin_test.explorer'].get_selected_zip
        self.patcher = patch.dict(sys.modules,self.modules)
        self.patcher.start()
        spec = importlib.util.spec_from_file_location('plugin_test',FILE,submodule_search_locations=[str(FILE.parent)])
        self.module = importlib.util.module_from_spec(spec)
        sys.modules['plugin_test'] = self.module
        spec.loader.exec_module(self.module)
        self.plugin = self.module.GlobalPlugin()
        self.plugin.show_library = Mock()
        self.plugin.scan_explorer = Mock()
    def tearDown(self):
        if hasattr(self,'plugin'):
            self.plugin.terminate()
            if hasattr(self.plugin, '_duration_executor'):
                self.plugin._duration_executor.shutdown(wait=True)
            self.plugin._executor.shutdown(wait=True)
        if hasattr(self,'patcher'): self.patcher.stop()
        sys.modules.pop('plugin_test',None)
    def later(self,delay,callback,*args):
        timer=Timer(delay,callback,*args); self.timers.append(timer); return timer
    def test_single_press_waits_then_opens_library(self):
        self.plugin.script_czytelnia(None)
        self.plugin.show_library.assert_not_called()
        self.timers[-1].fire()
        self.plugin.show_library.assert_called_once()
    def test_double_press_cancels_single_and_scans_once(self):
        self.plugin.script_czytelnia(None)
        single=self.timers[-1]
        self.repeat=1
        self.plugin.script_czytelnia(None)
        single.fire()
        self.plugin.show_library.assert_not_called()
        self.plugin.scan_explorer.assert_called_once()
        self.repeat=2
        self.plugin.script_czytelnia(None)
        self.plugin.scan_explorer.assert_called_once()
    def test_constructor_does_not_register_tools_menu_item(self):
        self.menu.Append.assert_not_called()
        self.modules['gui'].mainFrame.sysTrayIcon.Bind.assert_not_called()

    def test_termination_stops_pending_gesture_without_touching_tools_menu(self):
        self.plugin.script_czytelnia(None)
        pending=self.timers[-1]
        self.plugin.terminate()
        pending.fire()
        self.plugin.show_library.assert_not_called()
        self.menu.Remove.assert_not_called()
        self.modules['gui'].mainFrame.sysTrayIcon.Unbind.assert_not_called()
    def test_explorer_scan_passes_list_and_source_without_importing(self):
        paths = [ROOT / 'book.zip', ROOT / 'book-folder']
        self.modules['plugin_test.library'].scan_candidates.return_value = paths
        self.plugin._show_candidates = Mock()
        self.plugin.import_source = Mock()
        self.plugin._run = lambda fn, success: success(fn(None))
        self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.modules['plugin_test.explorer'].get_current_folder.assert_called_with('context')
        self.plugin._show_candidates.assert_called_once_with((ROOT, paths))
        self.plugin.import_source.assert_not_called()

    def test_open_audio_folder_confirms_directly_and_imports_only_after_yes(self):
        import tempfile
        spec = importlib.util.spec_from_file_location('folder_test_library', FILE.with_name('library.py'))
        library = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(library)
        wx = self.modules['wx']
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_QUESTION = 1, 2, 4
        wx.ID_YES, wx.ID_NO, wx.ID_CANCEL = 5103, 5104, 5101
        wx.MessageDialog = Mock()
        self.plugin._show_candidates = Mock()
        self.plugin.import_source = Mock()
        self.plugin._run = lambda fn, success: success(fn(None))
        self.module.scan_candidates = library.scan_candidates
        self.module.FolderSelectionRequired = library.FolderSelectionRequired
        work = ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            source = Path(temporary) / 'Książka. Tom 2'
            source.mkdir()
            (source / '01.mp3').write_bytes(b'audio test fixture')
            self.modules['plugin_test.explorer'].get_current_folder.return_value = source
            for answer in (wx.ID_NO, wx.ID_CANCEL, wx.ID_YES):
                with self.subTest(answer=answer):
                    wx.MessageDialog.reset_mock()
                    self.plugin.import_source.reset_mock()
                    dialog = Mock()
                    wx.MessageDialog.return_value = dialog
                    def respond(current):
                        self.assertIs(current, dialog)
                        self.plugin.import_source.assert_not_called()
                        return answer
                    self.plugin._modal = respond
                    self.module.GlobalPlugin.scan_explorer(self.plugin)
                    wx.MessageDialog.assert_called_once_with(
                        self.modules['gui'].mainFrame,
                        'Czy chcesz dodać do biblioteki: Książka. Tom 2?',
                        'Dodawanie książek do biblioteki',
                        style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                    self.plugin._show_candidates.assert_not_called()
                    dialog.SetYesNoLabels.assert_called_once_with('&Tak', '&Nie')
                    dialog.Destroy.assert_called_once()
                    if answer == wx.ID_YES:
                        self.plugin.import_source.assert_called_once_with(source)
                    else:
                        self.plugin.import_source.assert_not_called()

    def test_selected_zip_goes_straight_to_confirmation_without_scan_or_import(self):
        source = ROOT / 'Tytuł książki.ZIP'
        self.modules['plugin_test.explorer'].get_selected_zip.return_value = source
        self.plugin._confirm_import = Mock()
        self.plugin._show_candidates = Mock()
        self.plugin.import_source = Mock()
        self.plugin._run = lambda fn, success: success(fn(None))
        self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._confirm_import.assert_called_once_with(source)
        self.modules['plugin_test.library'].scan_candidates.assert_not_called()
        self.modules['plugin_test.explorer'].get_current_folder.assert_not_called()
        self.plugin._show_candidates.assert_not_called()
        self.plugin.import_source.assert_not_called()

    def test_zip_confirmation_imports_only_after_yes_and_always_destroys(self):
        self.assertTrue(hasattr(self.plugin, '_confirm_import'), 'Brak potwierdzenia importu ZIP')
        wx = self.modules['wx']
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_QUESTION = 1, 2, 4
        wx.ID_YES, wx.ID_NO, wx.ID_CANCEL = 5103, 5104, 5101
        wx.MessageDialog = Mock()
        source = ROOT / 'Tytuł książki.ZIP'
        self.plugin.import_source = Mock()
        for answer in (wx.ID_YES, wx.ID_NO, wx.ID_CANCEL):
            with self.subTest(answer=answer):
                dialog = Mock()
                wx.MessageDialog.reset_mock()
                wx.MessageDialog.return_value = dialog
                self.plugin.import_source.reset_mock()
                def respond(current):
                    self.assertIs(current, dialog)
                    self.plugin.import_source.assert_not_called()
                    return answer
                self.plugin._modal = respond
                self.plugin._confirm_import(source)
                wx.MessageDialog.assert_called_once_with(
                    self.modules['gui'].mainFrame,
                    'Czy chcesz dodać do biblioteki: Tytuł książki?',
                    'Dodawanie książek do biblioteki',
                    style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                dialog.SetYesNoLabels.assert_called_once_with('&Tak', '&Nie')
                dialog.Destroy.assert_called_once()
                if answer == wx.ID_YES:
                    self.plugin.import_source.assert_called_once_with(source)
                else:
                    self.plugin.import_source.assert_not_called()

    def test_parent_folder_speaks_instruction_and_empty_folder_keeps_empty_result(self):
        import tempfile
        spec = importlib.util.spec_from_file_location('folder_test_library', FILE.with_name('library.py'))
        library = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(library)
        self.module.scan_candidates = library.scan_candidates
        self.module.FolderSelectionRequired = library.FolderSelectionRequired
        self.plugin._confirm_import = Mock()
        self.plugin._show_candidates = Mock()
        self.plugin.import_source = Mock()
        self.plugin._run = lambda fn, success: success(fn(None))
        work = ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            parent = Path(temporary)
            self.modules['plugin_test.explorer'].get_current_folder.return_value = parent
            self.module.GlobalPlugin.scan_explorer(self.plugin)
            self.plugin._show_candidates.assert_called_once_with((parent, []))
            self.plugin._show_candidates.reset_mock()
            child = parent / 'Książka'
            child.mkdir()
            (child / '01.mp3').write_bytes(b'audio fixture')
            self.module.GlobalPlugin.scan_explorer(self.plugin)
            self.plugin._show_candidates.assert_not_called()
            self.modules['ui'].message.assert_called_once_with(
                'Folder zawiera podfolder. Otwórz go i zaznacz folder z nagraniami.')
            self.plugin._confirm_import.assert_not_called()
            self.plugin.import_source.assert_not_called()

    def test_navigation_while_scanning_audio_folder_refuses_confirmation(self):
        self.modules['plugin_test.library'].scan_candidates.return_value = [ROOT]
        self.modules['plugin_test.explorer'].get_current_folder.side_effect = [ROOT, ROOT.parent]
        self.plugin._confirm_import = Mock()
        self.plugin._run = lambda fn, success: success(fn(None))
        with self.assertRaises(RuntimeError):
            self.module.GlobalPlugin.scan_explorer(self.plugin)
        self.plugin._confirm_import.assert_not_called()

    def test_navigation_while_scanning_refuses_old_list(self):
        self.modules['plugin_test.library'].scan_candidates.return_value = []
        self.modules['plugin_test.explorer'].get_current_folder.side_effect = [ROOT, ROOT.parent]
        self.plugin._run = lambda fn, success: success(fn(None))
        with self.assertRaises(RuntimeError):
            self.module.GlobalPlugin.scan_explorer(self.plugin)

    def test_toggle_captures_selected_book_for_cold_start_on_ui_thread(self):
        controller = Mock()
        self.plugin._dialog = Mock()
        self.plugin._dialog.selected.return_value = {'id': 'chosen', 'title': 'Wybrana'}
        jobs = []
        def run(fn, *, resume_adjustment):
            self.assertFalse(resume_adjustment)
            jobs.append(fn)
        self.plugin._run = run
        self.plugin.action('toggle')
        self.plugin._dialog.selected.return_value = {'id': 'changed-after-click'}
        jobs.pop()(controller)
        controller.toggle.assert_called_once_with('chosen')

    def test_start_book_does_not_add_generic_loading_announcement(self):
        self.plugin._run = Mock()
        self.plugin.start_book('chosen')
        self.modules['ui'].message.assert_not_called()

    def test_transition_waits_one_second_before_message_and_before_resume(self):
        self.assertTrue(hasattr(self.plugin, '_transition'), 'Brak opoznionego przejscia')
        self.modules['wx'].CallLater = self.later
        self.plugin._dispatch_event = Mock()
        resume = Mock()
        queue = []
        self.modules['wx'].CallAfter = lambda fn, *args: queue.append(lambda: fn(*args))
        previous_timers = len(self.timers)
        self.plugin._transition('Wczytuję Rozdział 2.mp3.', lambda: True, resume)
        self.assertEqual(len(self.timers), previous_timers)
        queue.pop(0)()
        first = self.timers[-1]
        self.assertEqual(first.delay, 1000)
        self.modules['ui'].message.assert_not_called()
        first.fire()
        self.modules['ui'].message.assert_called_once_with('Wczytuję Rozdział 2.mp3.')
        second = self.timers[-1]
        self.assertIsNot(second, first)
        self.assertEqual(second.delay, 1000)
        self.plugin._dispatch_event.assert_not_called()
        second.fire()
        self.plugin._dispatch_event.assert_called_once_with(resume)
        self.assertFalse(self.plugin._transition_timers)

    def test_transition_rechecks_automatic_speech_without_blocking_resume(self):
        self.modules['wx'].CallLater = self.later
        self.plugin._dispatch_event = Mock()
        prefs = self.modules['plugin_test.preferences'].Preferences.return_value
        prefs.automatic_messages = True
        resume = Mock()
        self.plugin._transition('Odcinek 2.', lambda: True, resume)
        prefs.automatic_messages = False
        self.timers[-1].fire()
        self.modules['ui'].message.assert_not_called()
        self.timers[-1].fire()
        self.plugin._dispatch_event.assert_called_once_with(resume)

    def test_transition_cancellation_suppresses_message_or_resume(self):
        self.assertTrue(hasattr(self.plugin, '_transition'), 'Brak opoznionego przejscia')
        self.modules['wx'].CallLater = self.later
        self.plugin._dispatch_event = Mock()
        for stage in ('before-message', 'after-message'):
            with self.subTest(stage=stage):
                current = [True]
                self.modules['ui'].message.reset_mock()
                self.plugin._transition('Wczytuję 2.mp3.', lambda: current[0], Mock())
                first = self.timers[-1]
                if stage == 'after-message':
                    first.fire()
                current[0] = False
                self.timers[-1].fire()
                if stage == 'before-message':
                    self.modules['ui'].message.assert_not_called()
                self.plugin._dispatch_event.assert_not_called()
        self.plugin._transition('Wczytuję 3.mp3.', lambda: True, Mock())
        timer = self.timers[-1]
        self.plugin.terminate()
        self.assertTrue(timer.stopped)
        self.plugin._dispatch_event.assert_not_called()

    def test_automatic_messages_setting_is_immediate_and_keeps_manual_speech(self):
        self.assertTrue(hasattr(self.plugin, 'get_automatic_messages'))
        prefs = self.modules['plugin_test.preferences'].Preferences.return_value
        prefs.automatic_messages = True
        prefs.set_automatic_messages.side_effect = lambda value: setattr(prefs, 'automatic_messages', value)
        self.plugin.set_automatic_messages(False)
        self.assertFalse(self.plugin.get_automatic_messages())
        self.modules['ui'].message.assert_called_with('Komunikaty automatyczne wyłączone.')
        self.modules['ui'].message.reset_mock()
        self.plugin._say_auto('Koniec odcinka 2.')
        self.modules['ui'].message.assert_not_called()
        self.plugin._say('Błąd testowy.')
        self.modules['ui'].message.assert_called_once_with('Błąd testowy.')
        self.plugin.set_automatic_messages(True)
        self.plugin._say_auto('Odcinek 3.')
        self.modules['ui'].message.assert_called_with('Odcinek 3.')
        prefs.set_automatic_messages.side_effect = OSError('test zapisu')
        with self.assertRaises(OSError):
            self.plugin.set_automatic_messages(False)
        self.assertTrue(self.plugin.get_automatic_messages())

    def test_automatic_speech_checks_preference_at_delivery(self):
        self.assertTrue(hasattr(self.plugin, '_say_auto'))
        queue = []
        self.modules['wx'].CallAfter = lambda fn, *args: queue.append((fn, args))
        prefs = self.modules['plugin_test.preferences'].Preferences.return_value
        prefs.automatic_messages = True
        self.plugin._say_auto('Koniec odcinka 2.')
        prefs.automatic_messages = False
        for fn, args in queue:
            fn(*args)
        self.modules['ui'].message.assert_not_called()

    def test_preferences_work_without_registering_a_settings_panel(self):
        self.modules['plugin_test.settings_panel'].register.assert_not_called()
        self.assertTrue(self.plugin.get_auto_next())
        prefs = self.modules['plugin_test.preferences'].Preferences.return_value
        prefs.set_auto_next.side_effect = lambda value: setattr(prefs, 'auto_next', value)
        self.plugin._dialog = Mock()
        self.plugin._controller = Mock()
        self.plugin._controller.effective_auto_next.side_effect = lambda: prefs.auto_next
        self.plugin._dispatch_event = lambda fn: fn()
        self.plugin.set_auto_next(False)
        self.assertFalse(self.plugin.get_auto_next())
        self.assertEqual(self.modules['ui'].message.call_args.args, ('Przechodzenie do następnego odcinka wyłączone.',))
        self.plugin.set_auto_next(True)
        self.assertEqual(self.modules['ui'].message.call_args.args, ('Przechodzenie do następnego odcinka włączone.',))
        self.plugin._controller.auto_next_changed.assert_any_call(False)
        self.plugin.terminate()
        self.modules['plugin_test.settings_panel'].unregister.assert_not_called()

    def test_sleep_dialog_power_requires_yes_and_does_not_execute_power_now(self):
        self.assertTrue(hasattr(self.plugin, '_show_sleep_timer'))
        wx = self.modules['wx']
        wx.ID_OK, wx.ID_CANCEL, wx.ID_YES, wx.ID_NO = 5100, 5101, 5103, 5104
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_WARNING = 1, 2, 4
        wx.MessageDialog = Mock()
        wx.CallLater = self.later
        controller = Mock()
        controller.timer_state.return_value = {'mode':'minutes','action':'shutdown','remaining':900}
        def run(fn, success=None):
            result = fn(controller)
            if success: success(result)
        self.plugin._run = run
        for answer in (wx.ID_NO, wx.ID_YES):
            controller.reset_mock()
            dialog = Mock()
            dialog.config.return_value = {'minutes':15,'action':'shutdown'}
            self.modules['plugin_test.dialogs'].SleepTimerDialog.return_value = dialog
            self.plugin._modal = Mock(side_effect=[wx.ID_OK, answer])
            self.plugin._show_sleep_timer(None)
            if answer == wx.ID_YES:
                controller.arm_timer.assert_called_once_with(minutes=15, action='shutdown')
            else:
                controller.arm_timer.assert_not_called()
            self.modules['plugin_test.power'].perform.assert_not_called()
            dialog.Destroy.assert_called_once()
        self.assertTrue(wx.MessageDialog.call_args.kwargs['style'] & wx.NO_DEFAULT)

    def test_sleep_poll_dispatches_in_worker_and_stops_when_inactive(self):
        self.assertTrue(hasattr(self.plugin, '_poll_sleep_timer'))
        self.modules['wx'].CallLater = self.later
        self.plugin._controller = Mock()
        self.plugin._controller.timer_state.return_value = None
        self.plugin._executor.shutdown(wait=True)
        self.plugin._executor = Mock()
        self.plugin._executor.submit.side_effect = lambda fn: fn()
        self.plugin._sleep_state_updated({'mode':'minutes'})
        timer = self.timers[-1]
        self.assertEqual(timer.delay, 1000)
        timer.fire()
        self.plugin._controller.check_timer.assert_called_once()
        self.assertIsNone(self.plugin._sleep_poll_timer)

    def _terminate_during_timer(self, stage):
        # Load only the real controller. NVDA, backend and power remain mocks.
        spec = importlib.util.spec_from_file_location('timer_controller_test',
            FILE.with_name('controller.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module.Controller = module.Controller
        player = self.modules['plugin_test.player'].Player.return_value
        player.position = 37.5
        timer = self.modules['plugin_test.sleep_timer'].SleepTimer.return_value
        timer.poll.return_value = 'shutdown'
        timer.snapshot.return_value = None
        controller = self.plugin._initialize()
        controller.book_id = 'abc'
        entered, release = threading.Event(), threading.Event()

        def block(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release timer worker')
            return 'shutdown' if stage == 'poll' else None

        if stage == 'power':
            guarded_power = controller.power

            def delayed_power(action):
                block()
                guarded_power(action)

            controller.power = delayed_power
        else:
            target = {'poll': timer.poll, 'pause': player.pause,
                      'save': controller.library.save_position}[stage]
            target.side_effect = block
        future = self.plugin._executor.submit(controller.check_timer)
        try:
            self.assertTrue(entered.wait(5), 'timer did not reach barrier')
            self.plugin.terminate()
            self.assertTrue(self.plugin._closed)
            self.assertFalse(controller.closed, 'close must still be queued behind timer')
        finally:
            release.set()
            self.plugin._executor.shutdown(wait=True)
        self.assertTrue(future.result(timeout=5))
        self.modules['plugin_test.power'].perform.assert_not_called()
        self.assertTrue(controller.closed)

    def test_terminate_before_timer_pause_prevents_power(self):
        self._terminate_during_timer('poll')

    def test_terminate_during_timer_pause_prevents_power(self):
        self._terminate_during_timer('pause')

    def test_terminate_during_timer_save_prevents_power(self):
        self._terminate_during_timer('save')

    def test_terminate_after_controller_check_but_before_system_call_prevents_power(self):
        self._terminate_during_timer('power')

    def test_initializer_wires_automatic_notifications_separately(self):
        self.plugin._initialize()
        self.assertIn('notify_auto', self.module.Controller.call_args.kwargs)
        self.assertIs(self.module.Controller.call_args.kwargs['notify_auto'].__self__, self.plugin)
        self.assertEqual(self.module.Controller.call_args.kwargs['notify_auto'].__func__, self.plugin._say_auto.__func__)

    def test_show_existing_library_selects_first_without_starting_playback(self):
        self.plugin._dialog = Mock()
        self.plugin._dialog.bookList.GetCount.return_value = 3
        self.module.GlobalPlugin.show_library(self.plugin)
        self.plugin._dialog.bookList.SetSelection.assert_called_once_with(0)
        self.plugin._dialog.bookList.SetFocus.assert_called_once_with()
        self.assertIsNone(self.plugin._controller)
        self.plugin._dialog.bookList.SetSelection.reset_mock()
        self.plugin._dialog.bookList.GetCount.return_value = 0
        self.module.GlobalPlugin.show_library(self.plugin)
        self.plugin._dialog.bookList.SetSelection.assert_not_called()

    def test_initialize_passes_live_cancel_signal_and_guarded_power(self):
        self.plugin._initialize()
        options = self.module.Controller.call_args.kwargs
        self.assertFalse(options['cancelled']())
        options['power']('sleep')
        self.modules['plugin_test.power'].perform.assert_called_once_with('sleep')
        self.plugin.terminate()
        self.assertTrue(options['cancelled']())
        options['power']('shutdown')
        self.modules['plugin_test.power'].perform.assert_called_once_with('sleep')

    def test_timer_single_press_reads_active_state_without_dialog(self):
        self.assertTrue(hasattr(self.plugin, 'sleep_timer_gesture'))
        controller = self.plugin._controller = Mock()
        controller.timer_state.return_value = {'mode': 'minutes'}
        controller.timer_status.return_value = 'Wyłącznik czasowy aktywny. Za 15 minut zatrzymanie odtwarzania.'
        self.plugin._show_sleep_timer = Mock()
        self.plugin.sleep_timer_gesture()
        self.modules['ui'].message.assert_called_once_with(controller.timer_status.return_value)
        self.timers[-1].fire()
        self.modules['ui'].message.assert_called_once_with(controller.timer_status.return_value)
        self.plugin._show_sleep_timer.assert_not_called()
        controller.cancel_timer.assert_not_called()

    def test_double_timer_press_interrupts_speech_before_cancel(self):
        controller = self.plugin._controller = Mock()
        controller.timer_state.return_value = {'mode': 'minutes'}
        controller.timer_status.return_value = 'Stan wyłącznika.'
        order = []
        self.modules['speech'].cancelSpeech.side_effect = lambda: order.append('interrupt')
        controller.cancel_timer.side_effect = lambda: order.append('cancel')
        self.plugin.sleep_timer_gesture()
        self.assertEqual(order, [])
        self.plugin.sleep_timer_gesture()
        self.assertEqual(order, ['interrupt', 'cancel'])

    def test_timer_double_press_cancels_even_while_worker_busy(self):
        self.assertTrue(hasattr(self.plugin, 'sleep_timer_gesture'))
        controller = self.plugin._controller = Mock()
        controller.timer_state.return_value = {'mode': 'tracks'}
        self.plugin._show_sleep_timer = Mock()
        self.plugin._busy = True
        self.plugin.sleep_timer_gesture()
        pending = self.timers[-1]
        self.plugin.sleep_timer_gesture()
        self.assertTrue(pending.stopped)
        pending.fire()
        controller.cancel_timer.assert_called_once_with()
        controller.timer_status.assert_called_once_with()
        self.plugin._show_sleep_timer.assert_not_called()
        # A third press inside the same multi-press window must not re-arm.
        self.plugin.sleep_timer_gesture()
        controller.cancel_timer.assert_called_once_with()
        self.plugin._show_sleep_timer.assert_not_called()

    def test_timer_inactive_press_opens_configuration_once_and_close_cancels_pending(self):
        self.assertTrue(hasattr(self.plugin, 'sleep_timer_gesture'))
        self.plugin._controller = None
        self.plugin._show_sleep_timer = Mock()
        self.plugin.sleep_timer_gesture()
        pending = self.timers[-1]
        self.plugin.sleep_timer_gesture()
        pending.fire()
        self.plugin._show_sleep_timer.assert_called_once_with(None)
        self.plugin._show_sleep_timer.reset_mock()
        self.plugin._sleep_key_block_until = 0
        self.plugin.sleep_timer_gesture()
        pending = self.timers[-1]
        self.plugin.terminate()
        pending.fire()
        self.plugin._show_sleep_timer.assert_not_called()

    def test_timer_button_active_state_is_spoken_not_modal(self):
        controller = self.plugin._controller = Mock()
        controller.timer_state.return_value = {'mode': 'end'}
        controller.timer_status.return_value = 'Wyłącznik czasowy aktywny.'
        self.plugin._show_sleep_timer = Mock()
        self.plugin.show_sleep_timer()
        self.modules['ui'].message.assert_called_once_with(controller.timer_status.return_value)
        self.plugin._show_sleep_timer.assert_not_called()

    def test_expired_timer_during_multi_press_never_opens_configuration(self):
        controller = self.plugin._controller = Mock()
        self.plugin._show_sleep_timer = Mock()
        for double in (False, True):
            self.plugin._sleep_key_block_until = 0
            self.modules['ui'].message.reset_mock()
            controller.timer_state.return_value = {'mode': 'minutes'}
            controller.timer_status.return_value = 'Wyłącznik czasowy aktywny.'
            self.plugin.sleep_timer_gesture()
            self.modules['ui'].message.assert_called_once_with('Wyłącznik czasowy aktywny.')
            self.modules['ui'].message.reset_mock()
            pending = self.timers[-1]
            controller.timer_state.return_value = None
            if double:
                self.plugin.sleep_timer_gesture()
            else:
                pending.fire()
            if double:
                self.modules['ui'].message.assert_called_once_with('Wyłącznik czasowy nieaktywny.')
            else:
                self.modules['ui'].message.assert_not_called()
            self.plugin._show_sleep_timer.assert_not_called()
            if double:
                controller.cancel_timer.assert_called_once_with()
            else:
                controller.cancel_timer.assert_not_called()

    def _real_controller(self):
        import test_controller as fixtures
        spec = importlib.util.spec_from_file_location('plugin_real_controller', FILE.with_name('controller.py'))
        self.controller_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.controller_module)
        controller = self.controller_module.Controller(
            fixtures.Library(), fixtures.Player, self.plugin._dispatch_event,
            self.plugin._say, cancelled=lambda: self.plugin._closed)
        self.c = self.plugin._controller = controller
        fixtures.ControllerTests.real_timer(self)
        self.plugin._executor.submit(controller.start, 'abc', 0).result(timeout=5)
        self.modules['wx'].CallLater = self.later
        return controller

    def test_double_press_invalidates_consumed_power_action_during_real_save(self):
        controller = self._real_controller()
        self.plugin._executor.submit(controller.arm_timer, minutes=1, action='shutdown').result(timeout=5)
        self.plugin._show_sleep_timer = Mock()
        entered, release = threading.Event(), threading.Event()
        original_save = controller.library.save_position
        def blocked_save(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('save not released')
            original_save(*args)
        with patch.object(controller.library, 'save_position', side_effect=blocked_save):
            with patch.object(self.module.time, 'monotonic', return_value=10):
                self.plugin.sleep_timer_gesture()  # The first press sees an active timer.
                pending = self.timers[-1]
                self.now += 60
                future = self.plugin._executor.submit(controller.check_timer)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertIsNone(controller.timer_state())  # Already consumed, not cancelled.
                    self.plugin.sleep_timer_gesture()  # UI must still invalidate its epoch.
                    pending.callback(*pending.args)
                    self.plugin._show_sleep_timer.assert_not_called()
                finally:
                    release.set()
                self.assertTrue(future.result(timeout=5))
        self.assertEqual(self.powers, [], 'cancelled pending shutdown must never reach power')

    def test_stale_single_callback_after_cancel_does_not_reopen_or_speak(self):
        controller = self.plugin._controller = Mock()
        controller.timer_state.return_value = {'mode': 'end'}
        self.plugin._show_sleep_timer = Mock()
        self.plugin.sleep_timer_gesture()
        pending = self.timers[-1]
        self.plugin.sleep_timer_gesture()
        # Some event loops may already have queued a callback despite Stop.
        pending.callback(*pending.args)
        self.plugin._show_sleep_timer.assert_not_called()
        controller.timer_status.assert_called_once_with()
        controller.cancel_timer.assert_called_once_with()

    def test_count_timer_power_confirmation_uses_new_sleep_name(self):
        wx = self.modules['wx']
        wx.ID_OK, wx.ID_CANCEL, wx.ID_YES = 5100, 5101, 5103
        wx.YES_NO, wx.NO_DEFAULT, wx.ICON_WARNING = 1, 2, 4
        wx.MessageDialog = Mock()
        dialog = self.modules['plugin_test.dialogs'].SleepTimerDialog.return_value
        dialog.config.return_value = {'minutes': None, 'tracks': 2, 'action': 'sleep'}
        self.plugin._modal = Mock(side_effect=[wx.ID_OK, wx.ID_YES])
        controller = Mock()
        controller.timer_state.return_value = {'mode': 'tracks'}
        self.plugin._run = lambda fn, success: success(fn(controller))
        self.plugin._show_sleep_timer(None)
        text = wx.MessageDialog.call_args.args[1]
        self.assertIn('przełączyć komputer w stan uśpienia', text)
        self.assertIn('po zakończeniu 2 odcinków', text)
        self.assertTrue(wx.MessageDialog.call_args.kwargs['style'] & wx.NO_DEFAULT)
        controller.arm_timer.assert_called_once_with(minutes=None, tracks=2, action='sleep')
        self.modules['plugin_test.power'].perform.assert_not_called()

    def test_timer_single_defers_modal_until_after_nvda_queue_returns(self):
        # core.callLater's callback runs INSIDE CorePump, unlike wx.CallLater.
        queued = []
        self.modules['wx'].CallAfter = lambda fn, *args: queued.append((fn, args))
        self.plugin._show_sleep_timer = Mock()
        self.plugin.sleep_timer_gesture()
        self.timers[-1].fire()  # Simulated NVDA eventQueue / CorePump.
        self.plugin._show_sleep_timer.assert_not_called()
        self.assertEqual(len(queued), 1)
        callback, args = queued.pop()
        callback(*args)  # Separate wx event, after the NVDA pump has returned.
        self.plugin._show_sleep_timer.assert_called_once_with(None)

    def test_queued_timer_dialog_is_cancelled_by_library_close_or_terminate(self):
        queued = []
        self.modules['wx'].CallAfter = lambda fn, *args: queued.append((fn, args))
        self.plugin._show_sleep_timer = Mock()
        for stop in ('library_close', 'terminate'):
            with self.subTest(stop=stop):
                self.plugin.show_sleep_timer()
                self.assertEqual(len(queued), 1)
                if stop == 'library_close':
                    self.plugin.dialog_closed(Mock())
                else:
                    self.plugin.terminate()
                callback, args = queued.pop()
                callback(*args)
                self.plugin._show_sleep_timer.assert_not_called()

    def test_timer_dialog_requests_are_coalesced_and_not_reentrant(self):
        queued = []
        self.modules['wx'].CallAfter = lambda fn, *args: queued.append((fn, args))
        def nested_event_loop(state):
            self.plugin.show_sleep_timer()
            self.plugin.sleep_timer_gesture()
            self.assertEqual(queued, [], 'A second modal was scheduled inside ShowModal')
        self.plugin._show_sleep_timer = Mock(side_effect=nested_event_loop)
        self.plugin.show_sleep_timer()
        self.plugin.show_sleep_timer()
        self.assertEqual(len(queued), 1, 'Duplicate queued dialog requests')
        callback, args = queued.pop()
        callback(*args)
        self.plugin._show_sleep_timer.assert_called_once_with(None)

    def test_queued_timer_dialog_rechecks_busy_and_active_state(self):
        queued = []
        self.modules['wx'].CallAfter = lambda fn, *args: queued.append((fn, args))
        self.plugin._show_sleep_timer = Mock()
        self.plugin.show_sleep_timer()
        self.plugin._busy = True
        callback, args = queued.pop()
        callback(*args)
        self.plugin._show_sleep_timer.assert_not_called()
        self.plugin._busy = False
        queued.clear()
        self.plugin.show_sleep_timer()
        controller = self.plugin._controller = Mock()
        controller.timer_state.return_value = {'mode': 'end'}
        controller.timer_status.return_value = 'Aktywny.'
        callback, args = queued.pop()
        callback(*args)
        self.plugin._show_sleep_timer.assert_not_called()
        while queued:
            callback, args = queued.pop(0)
            callback(*args)
        self.modules['ui'].message.assert_called_with('Aktywny.')

    def test_initialize_wires_import_measurement_to_bundled_ffprobe(self):
        self.plugin._initialize()
        options = self.module.Library.call_args.kwargs
        self.assertIn('duration_probe', options, 'Import must measure lengths with bundled ffprobe')
        source = ROOT / '.test-work' / 'synthetic.wav'
        self.assertEqual(options['duration_probe'](source), 12.5)
        self.module.probe_duration.assert_called_once_with(source, ROOT / 'addon/tools/ffprobe.exe')
        self.assertEqual(options['media_dir'], ROOT / '.test-work/isolated-localappdata/Czytelnia/biblioteka')

    def test_initialize_uses_tools_directory(self):
        self.plugin._initialize()
        factory = self.module.Controller.call_args.args[1]
        callback = Mock()
        factory(callback)
        self.module.Player.assert_called_once_with(
            ROOT / 'addon/tools/ffplay.exe', ROOT / 'addon/tools/ffprobe.exe',
            on_event=callback, diagnostic=self.plugin._write_diagnostic)

    def _blocked_book_probe(self):
        import queue
        import tempfile
        import test_controller as fixtures
        from test_persistent_durations import library, controller as backend
        work = ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'source'
        source.mkdir()
        for index in range(3):
            (source / '{}.mp3'.format(index)).write_bytes(b'media fixture')
        self.book_library = library.Library(root / 'config', duration_probe=lambda path: 120)
        book = self.book_library.import_book(source)
        other = root / 'other'
        other.mkdir()
        (other / '0.mp3').write_bytes(b'other media fixture')
        self.other_book_id = self.book_library.import_book(other)['id']
        self.book_id = book['id']
        self.book_tracks = book['tracks']
        controller = backend.Controller(self.book_library, fixtures.Player,
            self.plugin._dispatch_event, self.plugin._say, cancelled=lambda: self.plugin._closed)
        self.c = self.plugin._controller = controller
        fixtures.ControllerTests.real_timer(self)
        self.plugin._executor.submit(controller.start, self.book_id, 0).result(timeout=5)
        self.saved_positions = Mock(wraps=self.book_library.save_position)
        self.book_library.save_position = self.saved_positions
        self.duration_commits = Mock(wraps=self.book_library.commit_durations)
        self.book_library.commit_durations = self.duration_commits
        self.book_paths = [self.book_library.track_path(self.book_id, i) for i in range(3)]
        # Real signature changes force scans, without blocking EOF's next track.
        for index in (0, 2):
            self.book_paths[index].write_bytes(b'changed synthetic media fixture')
        self.modules['wx'].CallLater = self.later
        self.ui_queue = queue.Queue()
        self.modules['wx'].CallAfter = lambda fn, *args: self.ui_queue.put((fn, args))
        self.modules['ui'].message.reset_mock()
        entered, release = threading.Event(), threading.Event()
        def probe(path):
            entered.set()
            if not release.wait(5):
                raise AssertionError('duration probe not released')
            return 120
        return controller, entered, release, probe

    def _assert_book_metadata_unchanged(self):
        import json
        self.assertEqual(self.book_library.get_book(self.book_id)['tracks'], self.book_tracks)
        stored = json.loads(self.book_library.json_path.read_text(encoding='utf-8'))
        book = next(book for book in stored['books'] if book['id'] == self.book_id)
        self.assertEqual(book['tracks'], self.book_tracks)

    def _drain_ui(self):
        import queue
        while True:
            try:
                fn, args = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            fn(*args)

    def test_blocked_book_probe_does_not_starve_real_eof_dispatch(self):
        controller, entered, release, probe = self._blocked_book_probe()
        with patch.object(self.book_library, '_duration_probe', side_effect=probe):
            self.plugin.action('book_status')
            try:
                self.assertTrue(entered.wait(5))
                controller.player.position = 120
                controller.player.callback('ended', None)
                # A real executor barrier, not a mocked dispatch assertion.
                try:
                    self.plugin._executor.submit(lambda: None).result(timeout=1)
                except TimeoutError:
                    self.fail('duration probing starved the playback worker / EOF')
                self.assertEqual(controller.track_index, 1)
                self.assertTrue(controller.player.is_playing)
                self.saved_positions.assert_called_with(self.book_id, 1, 0)
                saved = self.book_library.get_book(self.book_id)
                self.assertEqual((saved['track_index'], saved['position']), (1, 0))
                self.assertFalse(self.plugin._busy)
            finally:
                release.set()
                # Join both real workers before restoring the probe or NVDA fakes.
                if hasattr(self.plugin, '_duration_executor'):
                    self.plugin._duration_executor.shutdown(wait=True)
                self.plugin._executor.submit(lambda: None).result(timeout=5)
                self._drain_ui()
        self.modules['ui'].message.assert_any_call(
            '4 minuty do końca książki.')

    def _finish_book_probe(self, release):
        release.set()
        if hasattr(self.plugin, '_duration_executor'):
            self.plugin._duration_executor.submit(lambda: None).result(timeout=5)
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self._drain_ui()

    def test_blocked_book_probe_does_not_starve_real_minutes_timer(self):
        controller, entered, release, probe = self._blocked_book_probe()
        self.plugin._executor.submit(controller.arm_timer, minutes=1, action='shutdown').result(timeout=5)
        self._drain_ui()
        with patch.object(self.book_library, '_duration_probe', side_effect=probe):
            self.plugin.action('book_status')
            try:
                self.assertTrue(entered.wait(5))
                self.now += 60
                self.plugin._poll_sleep_timer()  # The real plugin poll, including its busy gate.
                try:
                    self.plugin._executor.submit(lambda: None).result(timeout=1)
                except TimeoutError:
                    self.fail('duration probing starved the minute timer')
                self.assertEqual(self.powers, ['shutdown'])  # Fake power only, after real save.
                self.assertFalse(controller.player.is_playing)
                self.saved_positions.assert_called_with(self.book_id, 0, 0)
                saved = self.book_library.get_book(self.book_id)
                self.assertEqual((saved['track_index'], saved['position']), (0, 0))
                self.assertIsNone(controller.timer_state())
            finally:
                self._finish_book_probe(release)

    def test_power_failure_is_still_announced_without_stop_success_message(self):
        controller, entered, release, probe = self._blocked_book_probe()
        self.plugin._executor.submit(controller.arm_timer, minutes=1, action='sleep').result(timeout=5)
        self._drain_ui()
        self.modules['ui'].message.reset_mock()
        controller.power = Mock(side_effect=OSError('synthetic power failure'))
        self.now += 60
        self.plugin._poll_sleep_timer()
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self._drain_ui()
        controller.power.assert_called_once_with('sleep')
        self.assertFalse(controller.player.is_playing)
        self.saved_positions.assert_called_once_with(self.book_id, 0, 0)
        self.assertIsNone(controller.timer_state())
        self.assertFalse(self.plugin._timer_poll_pending)
        self.modules['ui'].message.assert_called_once_with(
            'Nie udało się wykonać działania wyłącznika. synthetic power failure')
        self.modules['logHandler'].log.error.assert_called_once()

    def test_book_status_is_spoken_and_displayed_without_interrupting_playback(self):
        controller, entered, release, probe = self._blocked_book_probe()
        self.plugin._dialog = Mock()
        with patch.object(controller.player, 'pause', side_effect=AssertionError('status paused audio')):
            with patch.object(controller.player, 'load', side_effect=AssertionError('status loaded audio')):
                with patch.object(self.book_library, '_duration_probe', side_effect=probe):
                    self.plugin.action('book_status')
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertTrue(controller.player.is_playing)
                        self.saved_positions.assert_not_called()
                    finally:
                        self._finish_book_probe(release)
        text = '6 minut do końca książki.'
        self.modules['ui'].message.assert_called_once_with(text)
        self.plugin._dialog.status.ChangeValue.assert_called_once_with(text)

    def test_book_status_burst_has_one_scan_and_does_not_claim_busy(self):
        controller, entered, release, probe = self._blocked_book_probe()
        with patch.object(controller, 'book_status_snapshot', wraps=controller.book_status_snapshot) as snapshot:
            with patch.object(self.book_library, '_duration_probe', side_effect=probe) as run:
                self.plugin.action('book_status')
                try:
                    self.assertTrue(entered.wait(5))
                    for _ in range(100):
                        self.plugin.action('book_status')
                    self.plugin._executor.submit(lambda: None).result(timeout=1)
                    self.assertFalse(self.plugin._busy)
                    self.assertEqual(snapshot.call_count, 1)
                    self.assertEqual(run.call_count, 1)
                finally:
                    self._finish_book_probe(release)
                self.assertEqual(run.call_count, 2)  # Only the two changed files.
                self.assertFalse(self.plugin._book_status_pending)
                self.assertEqual(self.modules['ui'].message.call_count, 1)
                self.plugin.action('book_status')
                self.plugin._executor.submit(lambda: None).result(timeout=5)
                self._finish_book_probe(release)
                self.assertEqual(snapshot.call_count, 2)
                self.assertEqual(run.call_count, 2)  # Successful scan committed its cache.

    def test_book_change_during_probe_discards_cache_and_announcement(self):
        controller, entered, release, probe = self._blocked_book_probe()
        with patch.object(self.book_library, '_duration_probe', side_effect=probe) as run:
            self.plugin.action('book_status')
            try:
                self.assertTrue(entered.wait(5))
                self.plugin.start_book(self.other_book_id)
                self.plugin._executor.submit(lambda: None).result(timeout=1)
                self.assertEqual(controller.book_id, self.other_book_id)
                self._drain_ui()
                self.modules['ui'].message.reset_mock()
            finally:
                self._finish_book_probe(release)
            self.assertEqual(run.call_count, 1)  # Do not continue scanning the old book.
        self.duration_commits.assert_not_called()
        self._assert_book_metadata_unchanged()
        self.modules['ui'].message.assert_not_called()
        self.assertFalse(self.plugin._book_status_pending)

    def test_book_change_after_render_before_ui_delivery_discards_announcement(self):
        controller, entered, release, probe = self._blocked_book_probe()
        with patch.object(self.book_library, '_duration_probe', side_effect=probe):
            self.plugin.action('book_status')
            try:
                self.assertTrue(entered.wait(5))
            finally:
                release.set()
                self.plugin._duration_executor.submit(lambda: None).result(timeout=5)
                self.plugin._executor.submit(lambda: None).result(timeout=5)
            # Text is queued to wx, but the UI hasn't delivered it yet.
            self.plugin._executor.submit(controller.start, self.other_book_id).result(timeout=5)
            self.plugin._executor.submit(controller.start, self.book_id).result(timeout=5)
            self._drain_ui()
        messages = [call.args[0] for call in self.modules['ui'].message.call_args_list]
        self.assertFalse(any(text.endswith(' do końca książki.') or text == 'Czas do końca książki: nieznany.' for text in messages))

    def test_termination_during_probe_closes_player_without_late_cache_or_ui(self):
        controller, entered, release, probe = self._blocked_book_probe()
        with patch.object(self.book_library, '_duration_probe', side_effect=probe) as run:
            self.plugin.action('book_status')
            try:
                self.assertTrue(entered.wait(5))
                closed = threading.Event()
                original_close = controller.close
                def close():
                    original_close()
                    closed.set()
                controller.close = close
                self.plugin.terminate()
                self.assertTrue(closed.wait(1), 'shutdown queued behind duration probe')
                self.assertTrue(controller.closed)
                self.assertFalse(controller.player.is_playing)
                self.plugin.action('book_status')
            finally:
                release.set()
                self.plugin._duration_executor.shutdown(wait=True)
                self.plugin._executor.shutdown(wait=True)
                self._drain_ui()
            self.assertEqual(run.call_count, 1)
        self.duration_commits.assert_not_called()
        self._assert_book_metadata_unchanged()
        self.modules['ui'].message.assert_not_called()

    def test_probe_error_releases_request_gate_and_can_be_retried(self):
        controller, entered, release, probe = self._blocked_book_probe()
        release.set()
        with patch.object(self.book_library, '_duration_probe', side_effect=RuntimeError('probe failure')):
            self.plugin.action('book_status')
            self.plugin._executor.submit(lambda: None).result(timeout=5)
            self._finish_book_probe(release)
        self.assertFalse(self.plugin._book_status_pending)
        self.modules['ui'].message.assert_called_once_with('Nie udało się ustalić długości książki. probe failure')
        self.modules['ui'].message.reset_mock()
        with patch.object(self.book_library, '_duration_probe', side_effect=probe):
            self.plugin.action('book_status')
            self.plugin._executor.submit(lambda: None).result(timeout=5)
            self._finish_book_probe(release)
        self.modules['ui'].message.assert_called_once_with(
            '6 minut do końca książki.')

    def test_probe_owns_only_detached_data_and_cache_commits_on_playback_worker(self):
        controller, entered, release, probe = self._blocked_book_probe()
        owner = self.plugin._executor.submit(threading.get_ident).result(timeout=5)
        ui_owner = threading.get_ident()
        assert_equal = self.assertEqual
        class WorkerOnly:
            def __init__(self, target):
                self.target = target
            def __getattr__(self, name):
                assert_equal(threading.get_ident(), owner, 'live library/player read off worker')
                return getattr(self.target, name)
        controller.library = WorkerOnly(controller.library)
        controller.player = WorkerOnly(controller.player)
        self.modules['ui'].message.side_effect = lambda text: assert_equal(threading.get_ident(), ui_owner)
        original_write = self.book_library._write
        def checked_write(books):
            assert_equal(threading.get_ident(), owner, 'persistent metadata write off worker')
            return original_write(books)
        def checked_probe(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), owner)
            self.assertNotEqual(threading.get_ident(), ui_owner)
            return probe(*args, **kwargs)
        with patch.object(self.book_library, '_duration_probe', side_effect=checked_probe), \
             patch.object(self.book_library, '_write', side_effect=checked_write) as write:
            self.plugin.action('book_status')
            try:
                self.assertTrue(entered.wait(5))
                self._assert_book_metadata_unchanged()
                self.duration_commits.assert_not_called()
                write.assert_not_called()
            finally:
                self._finish_book_probe(release)
            write.assert_called_once()
        self.duration_commits.assert_called_once()
        # Reload from disk: the changed signatures, not a process-local cache, survive.
        restored = type(self.book_library)(self.book_library.json_path.parent,
            duration_probe=Mock(side_effect=AssertionError('unexpected repeat probe')))
        for index, path in enumerate(self.book_paths):
            info = path.stat()
            track = restored.get_book(self.book_id)['tracks'][index]
            self.assertEqual(track['signature'], {'size': info.st_size, 'mtime_ns': info.st_mtime_ns})
            self.assertEqual(restored.track_duration(self.book_id, index), 120)
        self.modules['ui'].message.assert_called_once_with(
            '6 minut do końca książki.')
        self.modules['logHandler'].log.error.assert_not_called()

    def test_termination_cancels_queued_scan_without_starting_probe(self):
        controller, entered, release, probe = self._blocked_book_probe()
        def occupy_duration_worker():
            entered.set()
            if not release.wait(5):
                raise AssertionError('duration queue not released')
        self.plugin._duration_executor.submit(occupy_duration_worker)
        self.assertTrue(entered.wait(5))
        closed = threading.Event()
        original_close = controller.close
        def close():
            original_close()
            closed.set()
        controller.close = close
        original_submit = self.plugin._duration_executor.submit
        queued = []
        def submit(*args, **kwargs):
            future = original_submit(*args, **kwargs)
            queued.append(future)
            return future
        with patch.object(self.plugin._duration_executor, 'submit', side_effect=submit):
            with patch.object(self.book_library, '_duration_probe', side_effect=probe) as run:
                try:
                    for _ in range(100):
                        self.plugin.action('book_status')
                    self.plugin._executor.submit(lambda: None).result(timeout=5)
                    self.assertEqual(len(queued), 1)
                    self.plugin.terminate()
                    self.assertTrue(queued[0].cancelled())
                    self.assertTrue(closed.wait(1))
                finally:
                    release.set()
                    self.plugin._duration_executor.shutdown(wait=True)
                    self.plugin._executor.shutdown(wait=True)
                    self._drain_ui()
                run.assert_not_called()
        self.duration_commits.assert_not_called()
        self._assert_book_metadata_unchanged()
        self.modules['ui'].message.assert_not_called()

    def test_removal_during_probe_discards_result(self):
        controller, entered, release, probe = self._blocked_book_probe()
        with patch.object(self.book_library, '_duration_probe', side_effect=probe):
            self.plugin.action('book_status')
            try:
                self.assertTrue(entered.wait(5))
                self.plugin._executor.submit(controller.remove, self.book_id).result(timeout=1)
                self.assertIsNone(controller.book_id)
            finally:
                self._finish_book_probe(release)
        self.duration_commits.assert_not_called()
        self.assertNotIn(self.book_id, [book['id'] for book in self.book_library.books()])
        restored = type(self.book_library)(self.book_library.addons_dir)
        self.assertNotIn(self.book_id, [book['id'] for book in restored.books()])
        self.modules['ui'].message.assert_not_called()
        self.plugin.action('book_status')
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self._finish_book_probe(release)
        self.modules['ui'].message.assert_called_once_with('Nie wybrano książki.')

    def test_boundary_navigation_never_writes_real_library_metadata(self):
        controller, entered, release, probe = self._blocked_book_probe()
        for name, index in (('previous_track', 0), ('next_track', 2)):
            with self.subTest(name=name):
                self.plugin._executor.submit(controller.start, self.book_id, index).result(timeout=5)
                self._drain_ui()
                self.assertTrue(self.plugin.adjust('seek_relative', 15))
                self.plugin._executor.submit(lambda: None).result(timeout=5)
                self._drain_ui()
                session = self.plugin._adjust_session
                before = vars(session).copy()
                contents = self.book_library.json_path.read_bytes()
                self.saved_positions.reset_mock()
                self.modules['ui'].message.reset_mock()
                with patch.object(self.book_library, '_write', wraps=self.book_library._write) as write:
                    self.plugin.action(name)
                    self.plugin._executor.submit(lambda: None).result(timeout=5)
                    self._drain_ui()
                    write.assert_not_called()
                self.saved_positions.assert_not_called()
                self.assertEqual(self.book_library.json_path.read_bytes(), contents)
                self.assertEqual(vars(session), before)
                self.assertIs(self.plugin._adjust_session, session)
                self.assertFalse(controller.player.is_playing)
                self.modules['ui'].message.assert_called_once_with(
                    'Początek książki.' if index == 0 else 'Koniec książki.')
                self.plugin.end_adjustment()
                self.plugin._executor.submit(lambda: None).result(timeout=5)
                self._drain_ui()
                self.assertTrue(controller.player.is_playing)

    def test_navigation_bounds_use_worker_state_after_queued_book_change(self):
        controller = self._real_controller()
        import queue
        self.ui_queue = queue.Queue()
        self.modules['wx'].CallAfter = lambda fn, *args: self.ui_queue.put((fn, args))
        self.assertTrue(self.plugin.adjust('seek_relative', 15))
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self._drain_ui()
        session = self.plugin._adjust_session
        entered, release = threading.Event(), threading.Event()
        def switch_track():
            entered.set()
            if not release.wait(5):
                raise AssertionError('queued track change not released')
            controller.start('abc', 2)
        self.plugin._executor.submit(switch_track)
        try:
            self.assertTrue(entered.wait(5))
            self.plugin.action('next_track')  # UI still sees index 0.
            self.assertFalse(session.released)
        finally:
            release.set()
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self._drain_ui()
        self.assertEqual(controller.track_index, 2)
        self.assertTrue(controller.player.is_playing)
        self.assertFalse(session.released, 'Boundary preflight must use current worker state')
        self.assertFalse(self.plugin._busy)
        self.modules['ui'].message.assert_called_with('Koniec książki.')

    def test_blocked_navigation_lookup_keeps_ui_responsive_and_honors_key_up(self):
        controller = self._real_controller()
        import queue
        self.ui_queue = queue.Queue()
        self.modules['wx'].CallAfter = lambda fn, *args: self.ui_queue.put((fn, args))
        self.assertTrue(self.plugin.adjust('seek_relative', 15))
        self.plugin._executor.submit(lambda: None).result(timeout=5)
        self._drain_ui()
        session = self.plugin._adjust_session
        owner = self.plugin._executor.submit(threading.get_ident).result(timeout=5)
        entered, release = threading.Event(), threading.Event()
        get_book = controller.library.get_book
        def blocked_read(key):
            self.assertEqual(threading.get_ident(), owner, 'Library read on UI thread')
            entered.set()
            if not release.wait(5):
                raise AssertionError('navigation lookup not released')
            return get_book(key)
        with patch.object(controller.library, 'get_book', side_effect=blocked_read):
            try:
                self.plugin.action('previous_track')
                self.assertTrue(entered.wait(5))
                self.assertFalse(session.released)
                self.plugin.end_adjustment()
                self.assertTrue(session.released)
                self.assertTrue(session.allow_resume)
                self.assertTrue(self.plugin._busy)
            finally:
                release.set()
            self.plugin._executor.submit(lambda: None).result(timeout=5)
            self._drain_ui()
            self.plugin._executor.submit(lambda: None).result(timeout=5)
            self._drain_ui()
        self.assertFalse(self.plugin._busy)
        self.assertTrue(session.done)
        self.assertTrue(controller.player.is_playing)
        self.assertEqual(self.plugin._adjust_sessions, {})

    def test_only_requested_global_gesture(self):
        self.assertEqual(self.module.GlobalPlugin._GlobalPlugin__gestures,
                         {'kb:NVDA+shift+c':'czytelnia'})


if __name__=='__main__': unittest.main()
