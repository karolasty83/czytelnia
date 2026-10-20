"""Folder imports never traverse descendants; archives retain their own policy."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load_library():
    spec = importlib.util.spec_from_file_location('folder_policy_library',
        ROOT / 'addon/globalPlugins/czytelnia/library.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FolderPolicyTests(unittest.TestCase):
    def setUp(self):
        self.module = load_library()
        work = ROOT / '.test-work'
        work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='folder-policy-', dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'Książka. Tom 2'
        self.source.mkdir()
        self.library = self.module.Library(self.root / 'data', duration_probe=lambda path: 2.0)

    def test_only_nested_audio_is_refused_even_for_one_child_without_writes(self):
        child = self.source / 'Nagrania'
        child.mkdir()
        (child / '01.mp3').write_bytes(b'nested fixture')
        message = 'Folder zawiera podfolder. Otwórz go i zaznacz folder z nagraniami.'
        with self.assertRaises(self.module.LibraryError) as caught:
            self.library.import_book(self.source)
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.library.books(), [])
        self.assertFalse(self.library.media_dir.exists())
        self.assertEqual((child / '01.mp3').read_bytes(), b'nested fixture')

    def test_two_subfolders_and_deep_chain_are_refused_without_traversal(self):
        for name in ('Pierwsza', 'Druga'):
            nested = self.source / name / 'Jeszcze głębiej'
            nested.mkdir(parents=True)
            (nested / '1.mp3').write_bytes(b'nested')
        original = Path.iterdir
        visited = []
        def immediate(path):
            visited.append(path)
            self.assertEqual(path, self.source.absolute())
            return original(path)
        with patch.object(Path, 'iterdir', immediate):
            with self.assertRaises(self.module.LibraryError) as caught:
                self.library.import_book(self.source)
        self.assertEqual(str(caught.exception),
            'Folder zawiera podfoldery. Otwórz go i zaznacz folder z nagraniami.')
        self.assertEqual(visited, [self.source.absolute()])
        self.assertFalse(self.library.media_dir.exists())

    def test_direct_audio_only_is_copied_sorted_and_nested_audio_stays_out(self):
        for name, content in [('10.mp3', b'ten'), ('2.MP3', b'two')]:
            (self.source / name).write_bytes(content)
        child = self.source / 'Inna książka'
        child.mkdir()
        (child / '1.mp3').write_bytes(b'must not be imported')
        book = self.library.import_book(self.source)
        self.assertEqual(book['title'], self.source.name)
        self.assertEqual([t['title'] for t in book['tracks']], ['2', '10'])
        self.assertEqual([self.library.track_path(book['id'], i).read_bytes()
                          for i in range(2)], [b'two', b'ten'])
        self.assertEqual(self.module.Library(self.root / 'data').books(), [book])
        self.assertEqual((child / '1.mp3').read_bytes(), b'must not be imported')

    def test_empty_folder_has_no_audio_message_not_subfolder_message(self):
        (self.source / 'cover.txt').write_text('notes')
        with self.assertRaisesRegex(self.module.LibraryError, 'Nie znaleziono plików dźwiękowych'):
            self.library.import_book(self.source)

    def test_irrelevant_large_non_audio_is_not_charged_to_audio_copy(self):
        (self.source / '1.mp3').write_bytes(b'audio')
        (self.source / 'unrelated.bin').write_bytes(b'x' * 100)
        with patch.object(self.module, 'MAX_FILE_BYTES', 10):
            book = self.library.import_book(self.source)
        self.assertEqual(len(book['tracks']), 1)

    def test_archive_nested_audio_import_is_unchanged(self):
        archive = self.root / 'Książka.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('CD1/1.mp3', b'one')
            bundle.writestr('CD2/2.mp3', b'two')
        book = self.library.import_book(archive)
        self.assertEqual([t['title'] for t in book['tracks']], ['1', '2'])
        self.assertEqual([self.library.track_path(book['id'], i).read_bytes()
                          for i in range(2)], [b'one', b'two'])

    def test_scan_never_offers_child_folders_as_books(self):
        child = self.source / 'Dziecko'
        child.mkdir()
        (child / '1.mp3').write_bytes(b'nested')
        with self.assertRaisesRegex(self.module.LibraryError, 'Folder zawiera podfolder'):
            self.module.scan_candidates(self.source)
        (self.source / '2.mp3').write_bytes(b'direct')
        self.assertEqual(self.module.scan_candidates(self.source), [self.source])


if __name__ == '__main__':
    unittest.main()
