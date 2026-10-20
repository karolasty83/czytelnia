"""Small no-GUI check for the timer's initially blank count."""
import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

FILE = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/dialogs.py'


class EmptyCountTests(unittest.TestCase):
    def test_count_starts_blank_and_requires_an_explicit_positive_number(self):
        tree = ast.parse(FILE.read_text(encoding='utf8'))
        timer = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SleepTimerDialog')
        assignment = next(n for n in ast.walk(timer) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Attribute) and t.attr == 'count' for t in n.targets))
        value = next(k.value for k in assignment.value.keywords if k.arg == 'value')
        self.assertEqual(ast.literal_eval(value), '')
        spec = importlib.util.spec_from_file_location('empty_count_dialog', FILE)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': types.SimpleNamespace(Dialog=object, Frame=object)}):
            spec.loader.exec_module(module)
        dialog = Mock(MINUTE_PRESETS=module.SleepTimerDialog.MINUTE_PRESETS,
                      ACTIONS=module.SleepTimerDialog.ACTIONS, MAX_TRACKS=10000)
        dialog.when.GetSelection.return_value = 1
        dialog.action.GetSelection.return_value = 0
        for empty in ('', '   '):
            dialog.count.GetValue.return_value = empty
            with self.assertRaises(ValueError) as error:
                module.SleepTimerDialog.config(dialog)
            self.assertEqual(str(error.exception), 'Wpisz liczbę odcinków.')
        dialog.count.GetValue.return_value = '3'
        self.assertEqual(module.SleepTimerDialog.config(dialog)['tracks'], 3)


if __name__ == '__main__':
    unittest.main()
