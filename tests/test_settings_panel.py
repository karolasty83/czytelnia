"""The removed NVDA settings category must not return as a shipped module."""
from pathlib import Path
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class RemovedSettingsPanelTests(unittest.TestCase):
    def test_settings_panel_source_is_absent(self):
        self.assertFalse((PROJECT / "addon/globalPlugins/czytelnia/settings_panel.py").exists())


if __name__ == "__main__":
    unittest.main()
