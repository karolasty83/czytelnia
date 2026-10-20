"""Explorer folder selection without controlling or modifying the desktop."""
import ctypes
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

FILE = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/czytelnia/explorer.py'


def window(frame, tab, path, filesystem=True):
    return SimpleNamespace(HWND=frame, tab=tab,
        Document=SimpleNamespace(Folder=SimpleNamespace(Self=SimpleNamespace(
            Path=path, IsFileSystem=filesystem))))


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(FILE.is_file(), 'Brak modułu odczytu Eksploratora')
        spec = importlib.util.spec_from_file_location('explorer_test', FILE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_apartment_releases_com_objects_before_uninitialize_on_errors(self):
        for failure in ('com', 'explorer', 'selection-com', 'selection-explorer'):
            with self.subTest(failure=failure):
                state = {'initialized': False, 'uninitialized': False}
                references = []
                releases = []

                class COMError(Exception):
                    pass

                class GUID(ctypes.Structure):
                    _fields_ = [('dummy', ctypes.c_int)]
                    def __init__(self, value):
                        super().__init__()

                class Owned:
                    def __init__(self):
                        references.append(weakref.ref(self))
                    def __del__(self):
                        releases.append(state['initialized'])

                class Item(Owned):
                    IsFileSystem = failure != 'explorer'
                    @property
                    def Path(self):
                        if failure == 'com':
                            # The exception chain and both failing frames own COM.
                            try:
                                raise COMError('inner COM failure')
                            except COMError as error:
                                raise COMError('outer COM failure') from error
                        return 'D:\\books'

                class SelectedItem(Owned):
                    IsFileSystem = True
                    @property
                    def Path(self):
                        try:
                            raise COMError('selected item inner COM failure')
                        except COMError as error:
                            raise COMError('selected item outer COM failure') from error

                class SelectedItems(Owned):
                    Count = 2 if failure == 'selection-explorer' else 1
                    def Item(self, index):
                        return SelectedItem()

                class Window(Owned):
                    HWND = 10
                    def __init__(self):
                        super().__init__()
                        self.Document = SimpleNamespace(Folder=SimpleNamespace(Self=Item()),
                            SelectedItems=SelectedItems)
                    def QueryInterface(self, interface):
                        return Provider()

                class Provider(Owned):
                    def QueryService(self, *args):
                        return Browser()

                class Browser(Owned):
                    def QueryInterface(self, interface):
                        return self
                    def GetWindow(self):
                        return 102

                class Windows(Owned):
                    Count = 1
                    def Item(self, index):
                        return Window()

                class Shell(Owned):
                    def Windows(self):
                        return Windows()

                def initialize():
                    state['initialized'] = True

                def uninitialize():
                    state['alive_at_uninitialize'] = sum(ref() is not None for ref in references)
                    state['initialized'] = False
                    state['uninitialized'] = True

                class IUnknown(ctypes.Structure):
                    _fields_ = []

                fake = SimpleNamespace(IUnknown=IUnknown, GUID=GUID,
                    COMMETHOD=lambda *args: None, HRESULT=ctypes.c_long,
                    COMError=COMError, CoInitialize=initialize, CoUninitialize=uninitialize)
                client = SimpleNamespace(CreateObject=lambda *args, **kwargs: Shell())
                caught = None
                with patch.dict(sys.modules, {'comtypes': fake, 'comtypes.client': client}):
                    try:
                        self.module._read_shell_path(10, 102, selection=failure.startswith('selection'))
                    except self.module.ExplorerError as error:
                        caught = error
                self.assertIsNotNone(caught)
                self.assertTrue(state['uninitialized'])
                self.assertEqual(state['alive_at_uninitialize'], 0,
                    'COM objects retained by the outgoing exception at CoUninitialize')
                self.assertTrue(releases)
                self.assertTrue(all(releases), 'COM object released outside initialized apartment')
                self.assertIsNone(caught.__cause__)
                self.assertIsNone(caught.__context__)
                expected = ('jeden plik ZIP' if failure == 'selection-explorer' else
                    'Nie udało się odczytać' if failure.endswith('com') else 'Otwórz zwykły folder')
                self.assertIn(expected, str(caught))
                frames = []
                tb = caught.__traceback__
                while tb:
                    frames.append(tb.tb_frame.f_code.co_name)
                    tb = tb.tb_next
                self.assertNotIn('_choose_folder', frames)

    def test_selected_source_apartment_preserves_com_cleanup(self):
        original = self.module._read_shell_path
        def read(*args, **kwargs):
            return original(*args, **kwargs, include_folders=True)
        with patch.object(self.module, '_read_shell_path', side_effect=read):
            self.test_apartment_releases_com_objects_before_uninitialize_on_errors()

    def test_selected_source_real_filesystem_folders_preserve_full_name(self):
        self.assertTrue(callable(getattr(self.module, 'get_selected_source', None)),
            'Missing selected folder/archive resolver')
        work = FILE.parents[3] / '.test-work/changes-1.0-folders'
        work.mkdir(parents=True, exist_ok=True)
        native = Native()
        context = self.module._capture(native, 10, 1002)
        with tempfile.TemporaryDirectory(prefix='explorer-source-', dir=work) as temp:
            parent = Path(temp)
            for name in ('Książka', 'Książka. Tom 2', 'folder.zip'):
                selected = parent / name
                selected.mkdir()
                with self.subTest(name=name), \
                     patch.object(self.module, '_native', return_value=native), \
                     patch.object(self.module, '_read_shell_path', return_value=(str(parent), str(selected))):
                    self.assertEqual(self.module.get_selected_source(context), selected)
                    self.assertEqual(self.module.get_selected_source(context).name, name)

    def test_selected_source_reads_physical_folder_from_active_shell_tab(self):
        chosen = window(10, 102, r'D:\books')
        item = SimpleNamespace(Path=r'D:\books\Książka. Tom 2', IsFileSystem=True, IsFolder=True)
        chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=1, Item=lambda index: item)
        windows = [window(10, 101, r'C:\wrong'), chosen, window(20, 102, r'C:\other')]
        native = Native()
        context = self.module._capture(native, 10, 1002)
        def read(frame, tab, **options):
            return self.module._choose_folder(windows, frame, tab, lambda w: w.tab, **options)
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', side_effect=read), \
             patch.object(self.module.os.path, 'isdir', return_value=True):
            self.assertEqual(self.module.get_selected_source(context), Path(item.Path))

    def test_selected_source_rejects_unsafe_scope_without_normalizing_traversal(self):
        for candidate, filesystem in (
                (r'D:\books\..\escape', True),
                (r'D:\books\nested\..\book', True),
                (r'D:\books\.\book', True),
                (r'D:\books\nested\book', True),
                (r'D:\outside\book', True),
                (r'D:\books', True),
                ('relative', True), ('D:book', True), (r'\books\book', True),
                ('::{GUID}', False), (r'D:\books\virtual', False)):
            chosen = window(10, 102, r'D:\books')
            item = SimpleNamespace(Path=candidate, IsFileSystem=filesystem, IsFolder=True)
            chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=1, Item=lambda index: item)
            with self.subTest(candidate=candidate, filesystem=filesystem), \
                 self.assertRaises(self.module.ExplorerError):
                self.module._choose_folder([chosen], 10, 102, lambda w: w.tab,
                    selection=True, include_folders=True)

    def test_selected_source_rejects_nonphysical_current_folder_even_without_selection(self):
        for path, filesystem in (('::{GUID}', False), ('relative', True),
                (r'\books', True), (r'D:\books\..\books', True)):
            chosen = window(10, 102, path, filesystem)
            chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=0)
            with self.subTest(path=path), self.assertRaises(self.module.ExplorerError):
                self.module._choose_folder([chosen], 10, 102, lambda w: w.tab,
                    selection=True, include_folders=True)

    def test_selected_source_multiselection_mentions_folder(self):
        for names in (('first', 'second'), ('first', 'book.zip'), ('audio.mp3', 'other.wav')):
            chosen = window(10, 102, r'D:\books')
            items = [SimpleNamespace(Path='D:\\books\\' + name, IsFileSystem=True) for name in names]
            chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=len(items), Item=items.__getitem__)
            with self.subTest(names=names), self.assertRaisesRegex(self.module.ExplorerError, 'jeden folder'):
                self.module._choose_folder([chosen], 10, 102, lambda w: w.tab,
                    selection=True, include_folders=True)

    def test_selected_source_archives_take_precedence_over_shell_folder_flag(self):
        for suffix in ('.ZIP', '.RAR', '.7z'):
            chosen = window(10, 102, r'D:\books')
            item = SimpleNamespace(Path='D:\\books\\book' + suffix, IsFileSystem=True, IsFolder=True)
            chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=1, Item=lambda index: item)
            native = Native()
            context = self.module._capture(native, 10, 1002)
            def read(frame, tab, **options):
                return self.module._choose_folder([chosen], frame, tab, lambda w: w.tab, **options)
            def isdir(path):
                self.assertEqual(path, r'D:\books', 'Do not mistake a ShellFolder archive for a directory')
                return True
            with self.subTest(suffix=suffix), \
                 patch.object(self.module, '_native', return_value=native), \
                 patch.object(self.module, '_read_shell_path', side_effect=read), \
                 patch.object(self.module.os.path, 'isdir', side_effect=isdir), \
                 patch.object(self.module.os.path, 'isfile', return_value=True):
                self.assertEqual(self.module.get_selected_source(context), Path(item.Path))

    def test_selected_source_real_files_and_no_selection(self):
        work = FILE.parents[3] / '.test-work/changes-1.0-folders'
        work.mkdir(parents=True, exist_ok=True)
        native = Native()
        context = self.module._capture(native, 10, 1002)
        with tempfile.TemporaryDirectory(prefix='explorer-files-', dir=work) as temp:
            parent = Path(temp)
            for name in (None, 'audio.mp3', 'other.txt', 'book.ZIP', 'book.RAR', 'book.7z', 'missing'):
                selected = parent / name if name else None
                if selected and name != 'missing':
                    selected.write_bytes(b'')
                with self.subTest(name=name), \
                     patch.object(self.module, '_native', return_value=native), \
                     patch.object(self.module, '_read_shell_path', return_value=(str(parent),
                         str(selected) if selected else None)) as read:
                    if name == 'missing':
                        with self.assertRaises(self.module.ExplorerError):
                            self.module.get_selected_source(context)
                    else:
                        expected = selected if name and name.startswith('book.') else None
                        self.assertEqual(self.module.get_selected_source(context), expected)
                        self.assertEqual(read.call_count, 2, 'Revalidate even empty/ordinary-file selection')

    def test_selected_source_revalidates_after_blocking_isdir_including_empty_selection(self):
        for selected in (None, r'D:\books\book', r'D:\books\audio.mp3'):
            for change in ('frame', 'tab', 'folder', 'selection', 'multiselection', 'deselected'):
                for check in ('parent', 'selected') if selected else ('parent',):
                    with self.subTest(selected=selected, change=change, check=check):
                        chosen = window(10, 102, r'D:\books')
                        items = [SimpleNamespace(Path=selected, IsFileSystem=True)] if selected else []
                        chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=len(items), Item=items.__getitem__)
                        native = Native()
                        context = self.module._capture(native, 10, 1002)
                        def read(frame, tab, **options):
                            return self.module._choose_folder([chosen], frame, tab, lambda w: w.tab, **options)
                        def isdir(path):
                            trigger = r'D:\books' if check == 'parent' else selected
                            if path == trigger:
                                if change == 'frame': native.front = 20
                                if change == 'tab': native.active = 101
                                if change == 'folder': chosen.Document.Folder.Self.Path = r'D:\other'
                                if change == 'selection':
                                    items[:] = [SimpleNamespace(Path=r'D:\books\other', IsFileSystem=True)]
                                if change == 'multiselection':
                                    items[:] = [SimpleNamespace(Path=r'D:\books\one', IsFileSystem=True)] * 2
                                if change == 'deselected':
                                    # An originally empty selection must also detect a newly selected item.
                                    items[:] = [] if selected else [SimpleNamespace(Path=r'D:\books\new', IsFileSystem=True)]
                            return not path.endswith('.mp3')
                        with patch.object(self.module, '_native', return_value=native), \
                             patch.object(self.module, '_read_shell_path', side_effect=read), \
                             patch.object(self.module.os.path, 'isdir', side_effect=isdir), \
                             patch.object(self.module.os.path, 'isfile', return_value=True), \
                             self.assertRaises(self.module.ExplorerError):
                            self.module.get_selected_source(context)

    def test_selected_source_refuses_archive_changes_during_blocking_isfile(self):
        with patch.object(self.module, 'get_selected_zip', self.module.get_selected_source), \
             patch.object(self.module.os.path, 'isdir', return_value=True):
            self.test_selected_zip_refuses_changes_during_blocking_isfile()

    def test_selected_source_checks_native_context_after_final_com_read(self):
        for selected in (None, r'D:\books\book'):
            for change in ('frame', 'tab'):
                native = Native()
                context = self.module._capture(native, 10, 1002)
                calls = []
                def read(*args, **kwargs):
                    calls.append(None)
                    if len(calls) == 2:
                        if change == 'frame': native.front = 20
                        else: native.active = 101
                    return r'D:\books', selected
                with self.subTest(selected=selected, change=change), \
                     patch.object(self.module, '_native', return_value=native), \
                     patch.object(self.module, '_read_shell_path', side_effect=read), \
                     patch.object(self.module.os.path, 'isdir', return_value=True), \
                     self.assertRaises(self.module.ExplorerError):
                    self.module.get_selected_source(context)
                self.assertEqual(len(calls), 2)

    def test_selected_source_refuses_current_archive_view_or_missing_directory(self):
        native = Native()
        context = self.module._capture(native, 10, 1002)
        for selected in (None, r'D:\book.zip\chapter'):
            with self.subTest(selected=selected), \
                 patch.object(self.module, '_native', return_value=native), \
                 patch.object(self.module, '_read_shell_path', return_value=(r'D:\book.zip', selected)), \
                 patch.object(self.module.os.path, 'isdir', return_value=False), \
                 self.assertRaises(self.module.ExplorerError):
                self.module.get_selected_source(context)

    def test_selected_zip_from_matching_active_tab_even_if_shell_folder(self):
        chosen = window(10, 102, 'D:\\books')
        item = SimpleNamespace(Path='D:\\books\\Książka.ZIP', IsFileSystem=True, IsFolder=True)
        chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=1, Item=lambda index: item)
        windows = [window(10, 101, 'C:\\wrong'), chosen, window(20, 102, 'C:\\other')]
        native = Native()
        context = self.module._capture(native, 10, 1002)
        def read(frame, tab, *, selection=False):
            return self.module._choose_folder(windows, frame, tab, lambda w: w.tab, selection=selection)
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', side_effect=read), \
             patch.object(self.module.os.path, 'isfile', return_value=True) as isfile:
            self.assertEqual(self.module.get_selected_zip(context), Path(item.Path))
        isfile.assert_called_once_with(item.Path)

    def test_selection_cardinality_type_and_filesystem_scope(self):
        def item(path, filesystem=True):
            return SimpleNamespace(Path=path, IsFileSystem=filesystem, IsFolder=True)
        cases = [
            ([], None),
            ([item('D:\\books\\audio.mp3')], None),
            ([item('D:\\books\\folder')], None),
            ([item('D:\\books\\one.zip'), item('D:\\books\\two.zip')], 'one'),
            ([item('D:\\books\\audio.mp3'), item('D:\\books\\one.zip')], 'one'),
            ([item('D:\\books\\one.zip', False)], 'scope'),
            ([item('one.zip')], 'scope'),
            ([item('D:one.zip')], 'scope'),
            ([item('\\books\\one.zip')], 'scope'),
            ([item('D:\\elsewhere\\one.zip')], 'scope'),
            ([item('D:\\books\\nested\\one.zip')], 'scope'),
            ([item('D:\\books\\..\\one.zip')], 'scope'),
            ([item('d:\\BOOKS\\one.zip')], 'd:\\BOOKS\\one.zip'),
        ]
        for items, expected in cases:
            with self.subTest(items=items, expected=expected):
                chosen = window(10, 102, 'D:\\books')
                chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=len(items), Item=items.__getitem__)
                if expected in ('one', 'scope'):
                    with self.assertRaises(self.module.ExplorerError) as error:
                        self.module._choose_folder([chosen], 10, 102, lambda w: w.tab, selection=True)
                    if expected == 'one':
                        self.assertIn('jeden plik ZIP', str(error.exception))
                else:
                    self.assertEqual(self.module._choose_folder([chosen], 10, 102,
                        lambda w: w.tab, selection=True), ('D:\\books', expected))

    def test_selected_zip_refuses_changes_during_blocking_isfile(self):
        for change in ('frame', 'tab', 'folder', 'file', 'deselected'):
            with self.subTest(change=change):
                native = Native()
                context = self.module._capture(native, 10, 1002)
                snapshot = ['D:\\books', 'D:\\books\\one.zip']
                def isfile(path):
                    if change == 'frame': native.front = 20
                    if change == 'tab': native.active = 101
                    if change == 'folder': snapshot[0] = 'D:\\other'
                    if change == 'file': snapshot[1] = 'D:\\books\\two.zip'
                    if change == 'deselected': snapshot[1] = None
                    return True
                with patch.object(self.module, '_native', return_value=native), \
                     patch.object(self.module, '_read_shell_path', side_effect=lambda *args, **kw: tuple(snapshot)), \
                     patch.object(self.module.os.path, 'isfile', side_effect=isfile):
                    with self.assertRaises(self.module.ExplorerError):
                        self.module.get_selected_zip(context)

    def test_selected_zip_rechecks_context_even_for_empty_selection(self):
        native = Native()
        context = self.module._capture(native, 10, 1002)
        def read(*args, **kwargs):
            native.active = 101
            return 'D:\\books', None
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', side_effect=read):
            with self.assertRaises(self.module.ExplorerError):
                self.module.get_selected_zip(context)

    def test_selected_zip_rechecks_context_after_final_com_read(self):
        native = Native()
        context = self.module._capture(native, 10, 1002)
        calls = []
        def read(*args, **kwargs):
            calls.append(None)
            if len(calls) == 2:
                native.active = 101
            return 'D:\\books', 'D:\\books\\one.zip'
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', side_effect=read), \
             patch.object(self.module.os.path, 'isfile', return_value=True):
            with self.assertRaises(self.module.ExplorerError):
                self.module.get_selected_zip(context)
        self.assertEqual(len(calls), 2)

    def test_selected_zip_refuses_multiselection_after_filesystem_check(self):
        chosen = window(10, 102, 'D:\\books')
        items = [SimpleNamespace(Path='D:\\books\\one.zip', IsFileSystem=True)]
        chosen.Document.SelectedItems = lambda: SimpleNamespace(Count=len(items), Item=items.__getitem__)
        native = Native()
        context = self.module._capture(native, 10, 1002)
        def read(frame, tab, *, selection=False):
            return self.module._choose_folder([chosen], frame, tab, lambda w: w.tab, selection=selection)
        def isfile(path):
            items.append(SimpleNamespace(Path='D:\\books\\two.zip', IsFileSystem=True))
            return True
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', side_effect=read), \
             patch.object(self.module.os.path, 'isfile', side_effect=isfile):
            with self.assertRaisesRegex(self.module.ExplorerError, 'jeden plik ZIP'):
                self.module.get_selected_zip(context)

    def test_selection_read_rechecks_folder_after_selecteditems(self):
        chosen = window(10, 102, 'D:\\books')
        def selected():
            chosen.Document.Folder.Self.Path = 'D:\\other'
            return SimpleNamespace(Count=1, Item=lambda index: SimpleNamespace(
                Path='D:\\books\\one.zip', IsFileSystem=True))
        chosen.Document.SelectedItems = selected
        with self.assertRaisesRegex(self.module.ExplorerError, 'Folder zmienił się'):
            self.module._choose_folder([chosen], 10, 102, lambda w: w.tab, selection=True)

    def test_selected_zip_none_and_missing_file(self):
        native = Native()
        context = self.module._capture(native, 10, 1002)
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', return_value=('D:\\books', None)), \
             patch.object(self.module.os.path, 'isfile') as isfile:
            self.assertIsNone(self.module.get_selected_zip(context))
            isfile.assert_not_called()
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', return_value=('D:\\books', 'D:\\books\\one.zip')), \
             patch.object(self.module.os.path, 'isfile', return_value=False):
            with self.assertRaises(self.module.ExplorerError):
                self.module.get_selected_zip(context)

    def test_selected_zip_real_isfile_rejects_directory_with_zip_suffix(self):
        work = FILE.parents[3] / '.test-work'
        work.mkdir(exist_ok=True)
        native = Native()
        context = self.module._capture(native, 10, 1002)
        with tempfile.TemporaryDirectory(prefix='selection-', dir=work) as temp:
            folder = Path(temp)
            archive = folder / 'one.ZIP'
            archive.write_bytes(b'')  # Filesystem type only; ZIP parsing is elsewhere.
            directory = folder / 'directory.zip'
            directory.mkdir()
            for selected in (archive, directory):
                with self.subTest(selected=selected), \
                     patch.object(self.module, '_native', return_value=native), \
                     patch.object(self.module, '_read_shell_path', return_value=(str(folder), str(selected))):
                    if selected == archive:
                        self.assertEqual(self.module.get_selected_zip(context), archive)
                    else:
                        with self.assertRaises(self.module.ExplorerError):
                            self.module.get_selected_zip(context)

    def test_select_active_tab_not_first_window_with_same_frame(self):
        windows = [window(10, 101, 'C:\\wrong'), window(10, 102, 'D:\\Książki'),
                   window(20, 201, 'C:\\other')]
        result = self.module._choose_folder(windows, 10, 102, lambda w:w.tab)
        self.assertEqual(result, 'D:\\Książki')

    def test_no_match_ambiguous_virtual_and_relative_are_rejected(self):
        cases = [[], [window(10,102,'D:\\a'),window(10,102,'D:\\b')],
                 [window(10,102,'::{GUID}',False)], [window(10,102,'relative')]]
        for windows in cases:
            with self.subTest(windows=windows), self.assertRaises(self.module.ExplorerError):
                self.module._choose_folder(windows,10,102,lambda w:w.tab)

    def test_capture_and_worker_recheck(self):
        native = Native()
        context = self.module._capture(native,10,1002)
        self.assertEqual((context.frame,context.tab),(10,102))
        with patch.object(self.module,'_native',return_value=native), \
             patch.object(self.module,'_read_shell_path',return_value='D:\\Książki'), \
             patch.object(self.module.os.path,'isdir',return_value=True):
            self.assertEqual(str(self.module.get_current_folder(context)),'D:\\Książki')
            native.active=101
            with self.assertRaises(self.module.ExplorerError): self.module.get_current_folder(context)

    def test_outside_explorer_wrong_focus_or_ambiguous_tab_refused(self):
        for change in ('foreground','focus','tab'):
            native=Native()
            if change=='foreground': native.front=99
            if change=='tab': native.active=0
            with self.subTest(change=change), self.assertRaises(self.module.ExplorerError):
                self.module._capture(native,10,99 if change=='focus' else 1002)

    def test_stale_focus_under_inactive_tab_is_refused(self):
        native = Native()
        native.active = 101  # Focus 1002 is still a child of the now inactive 102.
        with self.assertRaisesRegex(self.module.ExplorerError, 'Karta Eksploratora zmieniła się'):
            self.module._capture(native, 10, 1002)

    def test_native_active_tab_requires_exactly_one_visible_tab(self):
        cases = [
            ({}, set(), 0),
            ({'ShellTabWindowClass': [101, 102]}, set(), 0),
            ({'ShellTabWindowClass': [101, 102]}, {101, 102}, 0),
            ({'ShellTabWindowClass': [101, 102]}, {102}, 102),
            ({'TabWindowClass': [103]}, {103}, 103),
        ]
        for children, visible, expected in cases:
            with self.subTest(children=children, visible=visible):
                def find(frame, previous, class_name, title):
                    self.assertEqual(frame, 10)
                    handles = children.get(class_name, [])
                    index = handles.index(previous) + 1 if previous else 0
                    return handles[index] if index < len(handles) else 0
                native = self.module._Native.__new__(self.module._Native)
                native.user32 = SimpleNamespace(FindWindowExW=find,
                    IsWindowVisible=lambda hwnd: hwnd in visible)
                self.assertEqual(native.active_tab(10), expected)

    def test_navigation_during_com_read_is_refused(self):
        native=Native(); context=self.module._capture(native,10,1002)
        def read(*args):
            native.active=103
            return 'D:\\Książki'
        with patch.object(self.module,'_native',return_value=native), \
             patch.object(self.module,'_read_shell_path',side_effect=read), \
             patch.object(self.module.os.path,'isdir',return_value=True):
            with self.assertRaises(self.module.ExplorerError): self.module.get_current_folder(context)

    def test_same_tab_navigation_during_filesystem_check_is_refused(self):
        native = Native()
        context = self.module._capture(native, 10, 1002)
        selected = {'path': 'D:\\booksA'}
        def isdir(path):
            self.assertEqual(path, 'D:\\booksA')
            selected['path'] = 'D:\\booksB'
            return True
        with patch.object(self.module, '_native', return_value=native), \
             patch.object(self.module, '_read_shell_path', side_effect=lambda *args: selected['path']), \
             patch.object(self.module.os.path, 'isdir', side_effect=isdir):
            with self.assertRaisesRegex(self.module.ExplorerError, 'Folder zmienił się'):
                self.module.get_current_folder(context)

    def test_zip_shell_directory_and_missing_directory_refused(self):
        native=Native(); context=self.module._capture(native,10,1002)
        with patch.object(self.module,'_native',return_value=native), \
             patch.object(self.module,'_read_shell_path',return_value='D:\\book.zip\\part'), \
             patch.object(self.module.os.path,'isdir',return_value=False):
            with self.assertRaises(self.module.ExplorerError): self.module.get_current_folder(context)


class Native:
    def __init__(self): self.front=10; self.active=102
    def foreground(self): return self.front
    def root(self,hwnd): return 10 if hwnd in (10,101,102,103,1002) else hwnd
    def class_name(self,hwnd):
        return {10:'CabinetWClass',101:'ShellTabWindowClass',102:'ShellTabWindowClass',103:'ShellTabWindowClass'}.get(hwnd,'Other')
    def active_tab(self,frame): return self.active
    def parent(self,hwnd): return {1002:102,102:10}.get(hwnd,0)


if __name__ == '__main__': unittest.main()
