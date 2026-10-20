import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia'


class TimeTextTests(unittest.TestCase):
    def test_polish_units_and_omitted_seconds(self):
        path = ROOT / 'time_text.py'
        self.assertTrue(path.is_file(), 'shared time_text.py is missing')
        spec = importlib.util.spec_from_file_location('time_text_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = [(0, '0 sekund'), (-5, '0 sekund'), (1, '1 sekunda'),
                 (4, '4 sekundy'), (5, '5 sekund'), (12, '12 sekund'),
                 (22, '22 sekundy'), (59.9, '59 sekund'), (60, '1 minuta'),
                 (240, '4 minuty'), (300, '5 minut'), (720, '12 minut'),
                 (1260, '21 minut'), (1320, '22 minuty'), (3600, '1 godzina'),
                 (7200, '2 godziny'), (18000, '5 godzin'),
                 (11099, '3 godziny i 4 minuty'), (3661, '1 godzina i 1 minuta')]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(module.format_time(seconds), expected)
        self.assertIn('accusative', __import__('inspect').signature(module.format_time).parameters)
        for seconds, expected in [(1, '1 sekundę'), (60, '1 minutę'),
                                  (3600, '1 godzinę'), (3660, '1 godzinę i 1 minutę'),
                                  (11040, '3 godziny i 4 minuty')]:
            self.assertEqual(module.format_time(seconds, accusative=True), expected)


if __name__ == '__main__':
    unittest.main()
