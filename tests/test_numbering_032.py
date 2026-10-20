"""Numbering metadata contracts, using only synthetic project-local fixtures."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
MODULES = PROJECT / 'addon/globalPlugins/czytelnia'
WORK = PROJECT / '.test-work/changes-0.1.32'


def load_module(name):
    spec = importlib.util.spec_from_file_location('numbering_032_' + name, MODULES / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LabelTests(unittest.TestCase):
    def test_legacy_labels_are_pure_and_one_based(self):
        self.assertTrue((MODULES / 'numbering.py').is_file(), 'Missing pure numbering helper')
        numbering = load_module('numbering')
        book = {'tracks': [{'title': 'a'}, {'title': 'b'}, {'title': 'c'}]}
        before = deepcopy(book)
        self.assertEqual([numbering.track_label(book, i) for i in range(3)],
                         ['Odcinek 1', 'Odcinek 2', 'Odcinek 3'])
        self.assertEqual(book, before)

    def test_origin_labels_single_or_multiple_previews_and_reset(self):
        numbering = load_module('numbering')
        for start, expected in (
                (0, ['Odcinek 1', 'Odcinek 2', 'Odcinek 3', 'Odcinek 4']),
                (1, ['Wstęp', 'Odcinek 1', 'Odcinek 2', 'Odcinek 3']),
                (2, ['Wstęp 1', 'Wstęp 2', 'Odcinek 1', 'Odcinek 2']),
                (3, ['Wstęp 1', 'Wstęp 2', 'Wstęp 3', 'Odcinek 1'])):
            with self.subTest(start=start):
                book = {'numbering_start': start, 'tracks': [{} for _ in range(4)]}
                before = deepcopy(book)
                self.assertEqual([numbering.track_label(book, i) for i in range(4)], expected)
                self.assertEqual(book, before)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module('library')
        WORK.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix='numbering-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addons = self.root / 'addons'
        self.addons.mkdir()
        self.path = self.addons / 'czytelnia_config.json'
        self.document = {'version': 1, 'books': []}
        for book_id in ('a' * 32, 'b' * 32):
            tracks = []
            media = self.addons / 'czytelnia-biblioteka' / book_id
            media.mkdir(parents=True)
            for index in range(4):
                path = media / f'{index:06d}.mp3'
                path.write_bytes(f'synthetic {book_id} {index}'.encode())
                info = path.stat()
                tracks.append({'path': path.name, 'title': f'Plik {index}',
                               'duration': 90.5 + index,
                               'signature': {'size': info.st_size, 'mtime_ns': info.st_mtime_ns}})
            self.document['books'].append({
                'id': book_id, 'title': 'Książka ' + book_id, 'source': 'synthetic-' + book_id,
                'tracks': tracks, 'track_index': 3, 'position': 22.75,
                'bookmarks': [{'id': 'c' * 32, 'name': 'Mój punkt', 'track_index': 1, 'position': 10.5}],
                'next_bookmark_number': 7, 'bookmark_return': {'track_index': 2, 'position': 31.25},
                'extension_data': {'nested': [1, 'preserve', True]}})
        self.path.write_text(json.dumps(self.document, ensure_ascii=False), encoding='utf-8')
        self.lib = self.module.Library(self.addons)
        self.book_id = self.document['books'][0]['id']

    def test_setting_origin_preserves_every_other_field_and_media(self):
        self.assertTrue(callable(getattr(self.lib, 'set_numbering_start', None)),
                        'Missing numbering metadata setter')
        media = self.addons / 'czytelnia-biblioteka'
        before_media = {str(p.relative_to(media)): (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in media.rglob('*.mp3')}
        expected = deepcopy(self.document)
        # A later choice replaces the origin, not a growing list of exclusions.
        for start in (1, 3, 2, 0):
            with self.subTest(start=start):
                detached_before = self.lib.get_book(self.book_id)
                self.lib.set_numbering_start(self.book_id, start)
                expected['books'][0]['numbering_start'] = start
                self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), expected)
                self.assertEqual(self.lib.books(), expected['books'])
                reloaded = self.module.Library(self.addons)
                self.assertEqual(reloaded.books(), expected['books'])
                self.assertEqual(self.lib.get_book('b' * 32), self.document['books'][1])
                self.assertNotEqual(detached_before, self.lib.get_book(self.book_id))
                self.assertEqual({str(p.relative_to(media)): (p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in media.rglob('*.mp3')}, before_media)
                self.lib = reloaded

    def test_setter_rejects_invalid_indices_without_mutation(self):
        before = self.path.read_bytes()
        books = self.lib.books()
        for invalid in (True, False, None, '1', 1.0, -1, 4, 999, [], {}, float('nan'), float('inf')):
            with self.subTest(value=invalid):
                with self.assertRaises(self.module.LibraryError):
                    self.lib.set_numbering_start(self.book_id, invalid)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.lib.books(), books)

    def test_legacy_json_defaults_to_zero_without_startup_writes_or_probes(self):
        before = self.path.read_bytes()
        stamp = self.path.stat().st_mtime_ns
        with patch.object(self.module.Library, '_write', side_effect=AssertionError('startup write')):
            legacy = self.module.Library(self.addons, duration_probe=lambda path: self.fail('startup probe'))
            book = legacy.get_book(self.book_id)
            self.assertEqual(book.get('numbering_start', 0), 0)
            self.assertEqual(legacy.books(), self.document['books'])
            numbering = load_module('numbering')
            self.assertEqual([numbering.track_label(book, i) for i in range(4)],
                             ['Odcinek 1', 'Odcinek 2', 'Odcinek 3', 'Odcinek 4'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)

    def test_failed_atomic_replace_preserves_memory_disk_and_allows_retry(self):
        self.lib.set_numbering_start(self.book_id, 1)
        before = self.path.read_bytes()
        books = self.lib.books()
        with patch.object(self.module.os, 'replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(self.module.LibraryError):
                self.lib.set_numbering_start(self.book_id, 3)
        self.assertEqual(self.lib.books(), books)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.module.Library(self.addons).books(), books)
        self.assertEqual(list(self.addons.glob('.czytelnia-*.tmp')), [])
        self.lib.set_numbering_start(self.book_id, 3)
        expected = deepcopy(books)
        expected[0]['numbering_start'] = 3
        self.assertEqual(self.module.Library(self.addons).books(), expected)

    def test_stale_writer_cannot_overwrite_external_changes(self):
        books = self.lib.books()
        changed = deepcopy(self.document)
        changed['books'][0]['position'] = 44.5
        self.path.write_text(json.dumps(changed), encoding='utf-8')
        before = self.path.read_bytes()
        with self.assertRaises(self.module.LibraryError):
            self.lib.set_numbering_start(self.book_id, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.lib.books(), books)

    def test_invalid_book_ids_cannot_modify_a_book(self):
        before = self.path.read_bytes()
        for book_id in (None, True, '', '../outside', 'd' * 32):
            with self.subTest(book_id=book_id):
                with self.assertRaises(self.module.LibraryError):
                    self.lib.set_numbering_start(book_id, 1)
                self.assertEqual(self.lib.books(), self.document['books'])
                self.assertEqual(self.path.read_bytes(), before)

    def test_numbering_changes_each_book_independently(self):
        self.lib.set_numbering_start(self.book_id, 1)
        self.lib.set_numbering_start('b' * 32, 3)
        self.lib.set_numbering_start(self.book_id, 0)
        expected = deepcopy(self.document)
        expected['books'][0]['numbering_start'] = 0
        expected['books'][1]['numbering_start'] = 3
        self.assertEqual(self.module.Library(self.addons).books(), expected['books'])

    def test_new_import_persists_explicit_default_numbering(self):
        source = self.root / 'new-book'
        source.mkdir()
        (source / 'track.mp3').write_bytes(b'synthetic audio')
        lib = self.module.Library(self.addons, duration_probe=lambda path: 12.0)
        book = lib.import_book(source)
        self.assertEqual(book.get('numbering_start'), 0)
        stored = json.loads(self.path.read_text(encoding='utf-8'))['books'][-1]
        self.assertEqual(stored['numbering_start'], 0)
        self.assertEqual(self.module.Library(self.addons).get_book(book['id']), book)

    def test_load_rejects_invalid_numbering_without_repair_or_rewrite(self):
        for invalid in (True, False, None, '1', 1.0, -1, 4, 999, [], {}, float('nan'), float('inf')):
            with self.subTest(value=invalid):
                document = deepcopy(self.document)
                document['books'][0]['numbering_start'] = invalid
                self.path.write_text(json.dumps(document), encoding='utf-8')
                before = self.path.read_bytes()
                with self.assertRaises(self.module.LibraryError):
                    self.module.Library(self.addons)
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
