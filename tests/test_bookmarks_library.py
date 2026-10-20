"""Real filesystem bookmark persistence, isolated below .test-work."""
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        'bookmarks_test_' + name, PROJECT / 'addon/globalPlugins/czytelnia' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


library = load_module('library')


class BookmarkFixture(unittest.TestCase):
    def setUp(self):
        work = PROJECT / '.test-work'
        work.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=work, prefix='bookmarks-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lib = library.Library(self.root / 'config', duration_probe=lambda path: 120.0)
        self.source = self.root / 'source'
        self.source.mkdir()
        for index in range(3):
            (self.source / ('%d.wav' % index)).write_bytes(b'controlled audio fixture')
        self.key = self.lib.import_book(self.source)['id']

    def reload(self):
        return library.Library(self.root / 'config', duration_probe=lambda path: 120.0)

    def progress(self):
        book = self.lib.get_book(self.key)
        return book['track_index'], book['position']


class BookmarksLibraryTests(BookmarkFixture):
    def test_add_multiple_persists_position_order_detached_bookmarks_without_progress_change(self):
        self.lib.save_position(self.key, 2, 90)
        before = self.lib.json_path.read_bytes()
        self.assertEqual(self.lib.bookmarks(self.key), [])
        self.assertEqual(self.lib.json_path.read_bytes(), before)  # legacy read is not migration
        late = self.lib.add_bookmark(self.key, 1, 20.25)
        early = self.lib.add_bookmark(self.key, 0, 5)
        middle = self.lib.add_bookmark(self.key, 1, 1)
        self.assertEqual([late['name'], early['name'], middle['name']],
                         ['Zakładka', 'Zakładka', 'Zakładka'])
        for mark in (late, early, middle):
            self.assertRegex(mark['id'], r'^[0-9a-f]{32}$')
            self.assertEqual(set(mark), {'id', 'name', 'track_index', 'position'})
        self.assertEqual(len({mark['id'] for mark in (late, early, middle)}), 3)
        expected = [early, middle, late]
        self.assertEqual(self.lib.bookmarks(self.key), expected)
        self.assertEqual(self.reload().bookmarks(self.key), expected)
        late['name'] = 'external mutation'
        result = self.lib.bookmarks(self.key)
        result[0]['position'] = 999
        result.clear()
        self.assertEqual(self.lib.bookmarks(self.key)[0]['position'], 5)
        self.assertEqual(self.lib.bookmarks(self.key)[-1]['name'], 'Zakładka')
        self.assertEqual(self.progress(), (2, 90))

    def test_rename_get_remove_are_detached_and_default_name_has_no_number(self):
        self.lib.save_position(self.key, 2, 90)
        first = self.lib.add_bookmark(self.key, 1, 20)
        second = self.lib.add_bookmark(self.key, 1, 20)
        renamed = self.lib.rename_bookmark(self.key, first['id'], '  Rozdział ważny  ')
        self.assertEqual(renamed, dict(first, name='Rozdział ważny'))
        renamed['name'] = 'external'
        got = self.lib.get_bookmark(self.key, first['id'])
        self.assertEqual(got['name'], 'Rozdział ważny')
        got['position'] = 999
        self.assertEqual(self.lib.get_bookmark(self.key, first['id'])['position'], 20)
        self.assertIsNone(self.lib.remove_bookmark(self.key, second['id']))
        self.assertEqual(self.progress(), (2, 90))
        self.lib = self.reload()
        self.assertEqual(self.lib.get_bookmark(self.key, first['id'])['name'], 'Rozdział ważny')
        self.lib.remove_bookmark(self.key, first['id'])
        self.lib = self.reload()
        self.assertEqual(self.lib.bookmarks(self.key), [])
        self.assertEqual(self.lib.add_bookmark(self.key, 0, 0)['name'], 'Zakładka')
        self.assertEqual(self.progress(), (2, 90))

    def test_position_order_ignores_names_and_preserves_old_entries_without_write(self):
        late = self.lib.add_bookmark(self.key, 2, 1)
        early = self.lib.add_bookmark(self.key, 0, 20)
        middle = self.lib.add_bookmark(self.key, 0, 40)
        self.lib.rename_bookmark(self.key, late['id'], 'AAA')
        self.lib.rename_bookmark(self.key, early['id'], 'ZZZ')
        self.lib.rename_bookmark(self.key, middle['id'], 'Zakładka 42')
        before = self.lib.json_path.read_bytes()
        self.lib = self.reload()
        rows = self.lib.bookmarks(self.key)
        self.assertEqual([b['id'] for b in rows], [early['id'], middle['id'], late['id']])
        self.assertEqual([b['name'] for b in rows], ['ZZZ', 'Zakładka 42', 'AAA'])
        self.assertEqual(self.lib.json_path.read_bytes(), before)
        tied = self.lib.add_bookmark(self.key, 0, 40)
        self.assertEqual([b['id'] for b in self.lib.bookmarks(self.key)],
                         [early['id'], middle['id'], tied['id'], late['id']])

    def test_invalid_names_and_positions_do_not_write(self):
        mark = self.lib.add_bookmark(self.key, 0, 1)
        before, books = self.lib.json_path.read_bytes(), self.lib.books()
        for name in ('', ' \t\n ', None, 1, True, [], {}):
            with self.subTest(name=name), self.assertRaises(library.LibraryError):
                self.lib.rename_bookmark(self.key, mark['id'], name)
        for track, position in [(True, 0), (-1, 0), (3, 0), (0.0, 1), ('0', 1),
                                (0, True), (0, -1), (0, float('nan')), (0, float('inf')),
                                (0, '1'), (0, None), (0, 10 ** 1000)]:
            with self.subTest(track=track, position=position), self.assertRaises(library.LibraryError):
                self.lib.add_bookmark(self.key, track, position)
        self.assertEqual(self.lib.books(), books)
        self.assertEqual(self.lib.json_path.read_bytes(), before)

    def test_corrupt_stored_bookmarks_are_rejected_without_touching_json(self):
        mark = self.lib.add_bookmark(self.key, 0, 1)
        valid = json.loads(self.lib.json_path.read_bytes())
        bad_lists = [None, {}, 'bad', [None], [dict(mark), dict(mark)]]
        for field, values in {
            'id': [None, '', 'A' * 32, 'a' * 31, '../escape', 123],
            'name': [None, '', ' ', ' untrimmed', 'trailing ', 123, True],
            'track_index': [None, -1, 3, True, 0.0, '0'],
            'position': [None, -1, True, '1', float('nan'), float('inf'), 10 ** 1000],
        }.items():
            missing = dict(mark)
            del missing[field]
            bad_lists.append([missing])
            bad_lists.extend([dict(mark, **{field: value})] for value in values)
        documents = []
        for value in bad_lists:
            document = deepcopy(valid)
            document['books'][0]['bookmarks'] = value
            documents.append(document)
        for value in (None, True, 0, -1, 1.0, '2', [], 1):
            document = deepcopy(valid)
            document['books'][0]['next_bookmark_number'] = value
            documents.append(document)
        for index, document in enumerate(documents):
            with self.subTest(case=index):
                data = json.dumps(document).encode('utf-8')
                self.lib.json_path.write_bytes(data)
                with self.assertRaises(library.LibraryError):
                    self.reload()
                self.assertEqual(self.lib.json_path.read_bytes(), data)
        self.lib.json_path.write_bytes(json.dumps(valid).encode('utf-8'))

    def test_uuid_collision_does_not_duplicate_or_replace_bookmark(self):
        mark = self.lib.add_bookmark(self.key, 0, 1)
        before = self.lib.json_path.read_bytes()
        with patch.object(library.uuid, 'uuid4') as create:
            create.return_value.hex = mark['id']
            with self.assertRaises(library.LibraryError):
                self.lib.add_bookmark(self.key, 1, 2)
        self.assertEqual(self.lib.bookmarks(self.key), [mark])
        self.assertEqual(self.lib.json_path.read_bytes(), before)

    def test_write_failures_preserve_memory_disk_and_next_number(self):
        mark = self.lib.add_bookmark(self.key, 0, 1)
        before, books = self.lib.json_path.read_bytes(), self.lib.books()
        operations = [lambda: self.lib.add_bookmark(self.key, 1, 2),
                      lambda: self.lib.rename_bookmark(self.key, mark['id'], 'new'),
                      lambda: self.lib.remove_bookmark(self.key, mark['id'])]
        for operation in operations:
            with self.subTest(operation=operation):
                with patch.object(library.os, 'replace', side_effect=OSError('disk full')):
                    with self.assertRaises(library.LibraryError):
                        operation()
                self.assertEqual(self.lib.books(), books)
                self.assertEqual(self.lib.json_path.read_bytes(), before)
                self.assertEqual(list(self.lib.addons_dir.glob('.czytelnia-*.tmp')), [])
        self.assertEqual(self.lib.add_bookmark(self.key, 2, 3)['name'], 'Zakładka')

    def test_external_corrupt_json_is_never_overwritten_by_bookmark_mutation(self):
        mark = self.lib.add_bookmark(self.key, 0, 1)
        books = self.lib.books()
        corrupt = b'{broken JSON'
        self.lib.json_path.write_bytes(corrupt)
        for operation in [lambda: self.lib.add_bookmark(self.key, 1, 2),
                          lambda: self.lib.rename_bookmark(self.key, mark['id'], 'new'),
                          lambda: self.lib.remove_bookmark(self.key, mark['id'])]:
            with self.assertRaises(library.LibraryError):
                operation()
            self.assertEqual(self.lib.books(), books)
            self.assertEqual(self.lib.json_path.read_bytes(), corrupt)

    def test_unknown_ids_and_book_scoping_do_not_change_library(self):
        mark = self.lib.add_bookmark(self.key, 0, 1)
        other_source = self.root / 'other'
        other_source.mkdir()
        (other_source / '1.wav').write_bytes(b'fixture')
        other = self.lib.import_book(other_source)['id']
        self.assertEqual(self.lib.add_bookmark(other, 0, 0)['name'], 'Zakładka')
        before = self.lib.json_path.read_bytes()
        for key, bookmark_id in [(self.key, 'f' * 32), (other, mark['id']),
                                 (self.key, '../x'), (self.key, None),
                                 ('../bad', mark['id']), ('f' * 32, mark['id'])]:
            for operation in [lambda: self.lib.get_bookmark(key, bookmark_id),
                              lambda: self.lib.rename_bookmark(key, bookmark_id, 'new'),
                              lambda: self.lib.remove_bookmark(key, bookmark_id)]:
                with self.subTest(key=key, bookmark_id=bookmark_id), self.assertRaises(library.LibraryError):
                    operation()
        self.assertEqual(self.lib.json_path.read_bytes(), before)

    def test_delete_and_reimport_removes_bookmarks_and_uses_plain_name(self):
        self.lib.add_bookmark(self.key, 0, 1)
        old_key = self.key
        self.lib.remove_book(old_key)
        self.lib = self.reload()
        with self.assertRaises(library.LibraryError):
            self.lib.bookmarks(old_key)
        self.key = self.lib.import_book(self.source)['id']
        self.assertNotEqual(self.key, old_key)
        self.assertEqual(self.lib.bookmarks(self.key), [])
        self.assertEqual(self.lib.add_bookmark(self.key, 0, 0)['name'], 'Zakładka')


if __name__ == '__main__':
    unittest.main()
