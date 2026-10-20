"""Real local files only, isolated from the user's library."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / 'addon/globalPlugins/czytelnia/error_log.py'

class ErrorLogTests(unittest.TestCase):
    def load(self):
        self.assertTrue(FILE.is_file(), 'Missing local error log implementation')
        spec = importlib.util.spec_from_file_location('isolated_error_log', FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_lazy_first_error_and_append(self):
        module = self.load()
        with tempfile.TemporaryDirectory(dir=ROOT / '.test-work') as directory:
            folder = Path(directory) / 'biblioteka'
            logger = module.ErrorLog(folder)
            self.assertFalse(folder.exists())
            self.assertTrue(logger.write('playback.start.volume', 'WinError 10053 — połączenie'))
            path = folder / 'czytelnia.log'
            first = path.read_text(encoding='utf-8')
            self.assertIn('playback.start.volume', first)
            self.assertIn('10053', first)
            self.assertRegex(first, r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
            self.assertTrue(logger.write('import', 'próba druga'))
            self.assertTrue(path.read_text(encoding='utf-8').startswith(first))
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ['czytelnia.log'])

    def test_rotation_and_long_utf8_record_are_bounded(self):
        module = self.load()
        with tempfile.TemporaryDirectory(dir=ROOT / '.test-work') as directory:
            logger = module.ErrorLog(directory, max_bytes=1024)
            self.assertTrue(logger.write('one', 'ą' * 10000))
            self.assertLessEqual(logger.path.stat().st_size, 1024)
            self.assertIn('[truncated]', logger.path.read_text(encoding='utf-8'))
            self.assertTrue(logger.write('two', 'b' * 800))
            self.assertTrue(logger.path.with_name('czytelnia.log.1').is_file())
            self.assertTrue(logger.write('three', 'c' * 800))
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 2)
            self.assertTrue(all(p.stat().st_size <= 1024 for p in files))
            self.assertIn('[two]', logger.path.with_name('czytelnia.log.1').read_text())
            self.assertIn('[three]', logger.path.read_text())

    def test_unwritable_target_does_not_raise(self):
        module = self.load()
        with tempfile.TemporaryDirectory(dir=ROOT / '.test-work') as directory:
            folder = Path(directory) / 'blocked'
            folder.write_text('not a directory')
            self.assertFalse(module.ErrorLog(folder).write('test', 'error'))
            self.assertEqual(folder.read_text(), 'not a directory')

    def test_concurrent_writes_are_complete(self):
        import concurrent.futures
        module = self.load()
        with tempfile.TemporaryDirectory(dir=ROOT / '.test-work') as directory:
            logger = module.ErrorLog(directory)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda n: logger.write('thread', f'entry-{n:03d}'), range(50)))
            self.assertTrue(all(results))
            text = logger.path.read_text()
            for n in range(50):
                self.assertEqual(text.count(f'entry-{n:03d}'), 1)

if __name__ == '__main__':
    unittest.main()
