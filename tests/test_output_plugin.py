"""Output selection contracts; no live NVDA, hardware writes or audible audio."""
from pathlib import Path
import os
import unittest
from unittest.mock import Mock, patch
import test_plugin


class OutputPluginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_plugin.PluginTests('test_single_press_waits_then_opens_library')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.p = self.fixture.plugin
        self.module = self.fixture.module
        self.p._preferences.output_device = ''
        self.player = Mock(output_device='', is_playing=False, position=12.5, duration=100)
        self.player.set_output_device.side_effect = lambda value: setattr(self.player, 'output_device', value)
        self.c = Mock(player=self.player, closed=False)
        self.c.check_timer.return_value = False
        self.p._controller = self.c
        self.p._dialog = Mock()
        self.p._dialog.IsBeingDeleted.return_value = False

    def test_real_json_and_real_player_factory_restore_then_missing_json_defaults(self):
        import importlib.util
        import tempfile
        def load(name):
            spec = importlib.util.spec_from_file_location('output_roundtrip_' + name,
                test_plugin.ROOT / 'addon/globalPlugins/czytelnia' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        prefs_type = load('preferences').Preferences
        player_type = load('player').Player
        work = test_plugin.ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            path = Path(folder) / 'settings.json'
            original = prefs_type(path)
            original.set_volume(73)
            original.set_speed(1.5)
            original.set_output_device('USB')
            self.p._preferences = prefs_type(path)
            with patch.object(self.module, 'Player', player_type), \
                 patch.object(self.module, 'list_output_devices', return_value=['USB']):
                first = self.p._make_player(Path('tools'), Mock())
                try:
                    self.assertEqual(first.output_device, 'USB')
                finally:
                    first.close()
                path.unlink()
                self.p._preferences = prefs_type(path)
                fresh = self.p._make_player(Path('tools'), Mock())
                try:
                    self.assertEqual(fresh.output_device, '')
                    self.assertFalse(path.exists(), 'reading missing JSON must not recreate it')
                finally:
                    fresh.close()

    def test_factory_restores_output_before_any_load(self):
        self.p._preferences.output_device = 'Słuchawki USB'
        with patch.object(self.module, 'list_output_devices', return_value=['Słuchawki USB'], create=True):
            player = self.p._make_player(Path('tools'), Mock())
        player.set_output_device.assert_called_once_with('Słuchawki USB')
        player.load.assert_not_called()

    def test_unavailable_saved_output_uses_default_without_overwriting_json(self):
        self.p._preferences.output_device = 'Odłączona karta'
        with patch.object(self.module, 'list_output_devices', return_value=[], create=True):
            player = self.p._make_player(Path('tools'), Mock())
        player.set_output_device.assert_called_once_with('')
        self.p._preferences.set_output_device.assert_not_called()
        self.assertIn('domyślne', self.fixture.modules['ui'].message.call_args.args[0])

    def test_selection_saves_existing_json_and_keeps_paused_audio_paused(self):
        with patch.object(self.module, 'list_output_devices', return_value=['USB'], create=True):
            self.p._set_output_device(self.c, 'USB')
        self.p._preferences.set_output_device.assert_called_once_with('USB')
        self.assertEqual(self.player.output_device, 'USB')
        self.player.play.assert_not_called()

    def test_playing_switch_resumes_at_same_position_after_save(self):
        self.player.is_playing = True
        order = []
        self.player.pause.side_effect = lambda: order.append('pause')
        self.player.set_output_device.side_effect = lambda value: order.append(value)
        self.p._preferences.set_output_device.side_effect = lambda value: order.append('save')
        self.player.play.side_effect = lambda: order.append('play')
        with patch.object(self.module, 'list_output_devices', return_value=['USB'], create=True):
            self.p._set_output_device(self.c, 'USB')
        self.assertEqual(order, ['pause', 'USB', 'save', 'play'])
        self.assertEqual(self.player.position, 12.5)
        self.player.load.assert_not_called()
        self.player.seek.assert_not_called()

    def test_save_failure_restores_previous_output_and_does_not_announce_success(self):
        self.player.output_device = 'Old'
        self.player.is_playing = True
        self.p._preferences.set_output_device.side_effect = OSError('disk full')
        with patch.object(self.module, 'list_output_devices', return_value=['USB'], create=True):
            with self.assertRaises(OSError):
                self.p._set_output_device(self.c, 'USB')
        self.assertEqual(self.player.output_device, 'Old')
        self.player.play.assert_called_once()
        self.fixture.modules['ui'].message.assert_not_called()

    def test_shutdown_during_save_never_resumes(self):
        self.player.is_playing = True
        self.p._preferences.set_output_device.side_effect = lambda value: setattr(self.p, '_closed', True)
        with patch.object(self.module, 'list_output_devices', return_value=['USB'], create=True):
            self.p._set_output_device(self.c, 'USB')
        self.player.play.assert_not_called()
        self.p._closed = False  # let fixture cleanup perform its normal shutdown

    def test_shutdown_while_checking_timer_never_resumes(self):
        self.player.is_playing = True
        def check():
            self.p._closed = True
            return False
        self.c.check_timer.side_effect = check
        with patch.object(self.module, 'list_output_devices', return_value=['USB'], create=True):
            self.p._set_output_device(self.c, 'USB')
        self.player.play.assert_not_called()
        self.p._closed = False

    def test_same_output_does_not_restart_but_saves_default_after_json_loss(self):
        self.player.is_playing = True
        self.p._set_output_device(self.c, '')
        self.p._preferences.set_output_device.assert_called_once_with('')
        self.player.pause.assert_not_called()
        self.player.play.assert_not_called()

    def test_disconnected_choice_is_not_saved_or_applied(self):
        with patch.object(self.module, 'list_output_devices', return_value=[], create=True):
            with self.assertRaises(ValueError):
                self.p._set_output_device(self.c, 'Gone')
        self.p._preferences.set_output_device.assert_not_called()
        self.player.pause.assert_not_called()

    def test_eof_during_switch_is_not_restarted_from_zero(self):
        self.player.is_playing = True
        self.player.pause.side_effect = lambda: setattr(self.player, 'position', 100)
        with patch.object(self.module, 'list_output_devices', return_value=['USB'], create=True):
            self.p._set_output_device(self.c, 'USB')
        self.player.play.assert_not_called()

    def _open(self, answer, selection=1):
        wx = self.fixture.modules['wx']
        wx.ID_OK, wx.ID_CANCEL = 5100, 5101
        dlg = Mock()
        dlg.GetSelection.return_value = selection
        wx.SingleChoiceDialog = Mock(return_value=dlg)
        self.p._modal = Mock(return_value=answer)
        self.p._run = Mock()
        token = self.p._output_request = object()
        self.p._open_output_devices(token, (['USB'], 'USB'))
        return dlg

    def test_output_choice_does_not_restart_or_write_before_confirmation(self):
        self.p._run = lambda fn, success=None, **kwargs: success(fn(self.c)) or True
        self.p._open_output_devices = Mock()
        with patch.object(self.module, 'list_output_devices', return_value=['USB'], create=True):
            self.p.show_output_devices()
        self.p._open_output_devices.assert_called_once()
        self.p._preferences.set_output_device.assert_not_called()
        self.player.pause.assert_not_called()
        self.player.play.assert_not_called()

    def test_repeated_request_is_bounded_and_failure_allows_retry(self):
        self.p._run = Mock(return_value=True)
        self.p.show_output_devices()
        self.p.show_output_devices()
        self.p._run.assert_called_once()
        self.p._run.call_args.kwargs['failure']()
        self.assertIsNone(self.p._output_request)
        self.p.show_output_devices()
        self.assertEqual(self.p._run.call_count, 2)

    def test_dialog_default_first_selection_enter_applies_only_selected_device(self):
        dlg = self._open(5100)
        call = self.fixture.modules['wx'].SingleChoiceDialog.call_args
        self.assertEqual(call.args[2], 'Wyjście dźwięku')
        self.assertEqual(call.args[3], ['Domyślne urządzenie systemowe', 'USB'])
        dlg.SetSelection.assert_called_once_with(1)
        dlg.Destroy.assert_called_once()
        self.assertEqual(self.p._run.call_count, 1)
        with patch.object(self.p, '_set_output_device') as apply:
            self.p._run.call_args.args[0](self.c)
        apply.assert_called_once_with(self.c, 'USB')
        self.p._dialog.bookList.SetFocus.assert_called_once()

    def test_escape_closes_dialog_without_saving_or_switching(self):
        dlg = self._open(5101)
        self.p._run.assert_not_called()
        self.p._preferences.set_output_device.assert_not_called()
        dlg.Destroy.assert_called_once()

    def test_closing_library_invalidates_delayed_dialog(self):
        token = self.p._output_request = object()
        parent = self.p._dialog
        self.p.dialog_closed(parent)
        self.p._open_output_devices(token, (['USB'], ''))
        self.assertIsNone(self.p._output_request)
        self.p._preferences.set_output_device.assert_not_called()


if __name__ == '__main__':
    unittest.main()
