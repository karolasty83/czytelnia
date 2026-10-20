"""Temporary count-timer state is visible to Shift+K, not persisted."""
import unittest
from unittest.mock import Mock
import test_plugin


class TimerAutoNextPluginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_plugin.PluginTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.p = self.fixture.plugin
        self.prefs = self.fixture.modules['plugin_test.preferences'].Preferences.return_value
        self.prefs.auto_next = False

    def test_effective_state_visible_but_saved_callback_remains_off(self):
        self.p._controller = Mock()
        self.p._controller.effective_auto_next.return_value = True
        self.assertTrue(self.p.get_auto_next())
        self.assertFalse(self.p._get_saved_auto_next())
        self.prefs.set_auto_next.assert_not_called()
        self.p._controller.effective_auto_next.return_value = False
        self.assertFalse(self.p.get_auto_next())

    def test_controller_receives_saved_not_effective_callback(self):
        self.p._initialize()
        factory = self.fixture.modules['plugin_test.controller'].Controller
        callback = factory.call_args.kwargs['auto_next']
        self.p._controller.effective_auto_next.return_value = True
        self.assertFalse(callback())

    def test_real_controller_override_and_shift_k_cancellation(self):
        import test_adjustment_plugin
        import test_timer_auto_next
        fixture = test_adjustment_plugin.AdjustmentPluginTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plugin, controller = fixture.p, fixture.core.c
        prefs = fixture.fixture.modules['plugin_test.preferences'].Preferences.return_value
        prefs.auto_next = False
        controller.auto_next = plugin._get_saved_auto_next
        controller.sleep_timer = test_timer_auto_next.sleep_timer.SleepTimer()
        plugin._dispatch_event = lambda fn: fn()
        controller.arm_timer(tracks=2)
        self.assertTrue(plugin.get_auto_next())
        self.assertFalse(prefs.auto_next)
        prefs.set_auto_next.assert_not_called()
        plugin.set_auto_next(not plugin.get_auto_next())
        self.assertIsNone(controller.timer_state())
        self.assertFalse(plugin.get_auto_next())
        prefs.set_auto_next.assert_called_once_with(False)
        controller.arm_timer(tracks=2)
        controller.cancel_timer()
        self.assertFalse(plugin.get_auto_next())

    def test_shift_k_style_toggle_requests_disable_of_temporary_state(self):
        self.p._controller = Mock()
        self.p._controller.effective_auto_next.return_value = True
        self.p._dispatch_event = lambda fn: fn()
        self.p.set_auto_next(not self.p.get_auto_next())
        self.p._controller.auto_next_changed.assert_called_once_with(False)
        self.prefs.set_auto_next.assert_called_once_with(False)


if __name__ == '__main__':
    unittest.main()
