"""Read the selected Explorer TAB, never an arbitrary window with same HWND.

Tab match follows Microsoft PowerToys FileExplorerHelper/HWNDExtensions:
https://github.com/microsoft/PowerToys/tree/5321ca867dcd1080131f150d3a14093cf853dade/src/modules/peek/Peek.UI
No clipboard, navigation, focus changes or cached-folder fallback.
"""
from collections import namedtuple
import ctypes
from ctypes import wintypes
import ntpath
import os
from pathlib import Path
import traceback

ExplorerContext = namedtuple('ExplorerContext', 'frame tab')
TAB_CLASSES = ('ShellTabWindowClass', 'TabWindowClass')


class _Native:
    def __init__(self):
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        signatures = {
            'GetForegroundWindow': ([], wintypes.HWND),
            'GetAncestor': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'FindWindowExW': ([wintypes.HWND,wintypes.HWND,wintypes.LPCWSTR,wintypes.LPCWSTR], wintypes.HWND),
            'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
        }
        for name,(args,result) in signatures.items():
            fn = getattr(self.user32,name)
            fn.argtypes, fn.restype = args,result

    def foreground(self):
        return int(self.user32.GetForegroundWindow() or 0)

    def root(self, hwnd):
        return int(self.user32.GetAncestor(hwnd,2) or 0) if hwnd else 0

    def parent(self, hwnd):
        return int(self.user32.GetAncestor(hwnd,1) or 0) if hwnd else 0

    def class_name(self, hwnd):
        buf = ctypes.create_unicode_buffer(256)
        return buf.value if self.user32.GetClassNameW(hwnd,buf,len(buf)) else ''

    def active_tab(self, frame):
        for class_name in TAB_CLASSES:
            found = []
            hwnd = 0
            while True:
                hwnd = self.user32.FindWindowExW(frame,hwnd,class_name,None)
                if not hwnd:
                    break
                found.append(int(hwnd))
            if found:
                visible = [h for h in found if self.user32.IsWindowVisible(h)]
                return visible[0] if len(visible) == 1 else 0
        return 0


def _native():
    return _Native()


def _capture(native, foreground_hwnd, focus_hwnd=0):
    frame = native.root(foreground_hwnd)
    if not frame or native.class_name(frame) != 'CabinetWClass':
        raise ExplorerError('Przejdź do folderu w Eksploratorze Windows i naciśnij skrót dwukrotnie.')
    if native.root(native.foreground()) != frame or (focus_hwnd and native.root(focus_hwnd) != frame):
        raise ExplorerError('Fokus nie znajduje się w tym oknie Eksploratora. Naciśnij skrót ponownie.')
    tab = native.active_tab(frame)
    if not tab:
        raise ExplorerError('Nie można rozpoznać aktywnej karty Eksploratora. Użyj Dodaj folder w bibliotece.')
    visited = set()
    hwnd = focus_hwnd
    while hwnd and hwnd != frame:
        if hwnd in visited:
            raise ExplorerError('Nie można rozpoznać fokusu Eksploratora.')
        visited.add(hwnd)
        if native.class_name(hwnd) in TAB_CLASSES and hwnd != tab:
            raise ExplorerError('Karta Eksploratora zmieniła się. Naciśnij skrót ponownie.')
        hwnd = native.parent(hwnd)
    return ExplorerContext(frame,tab)


def capture_context():
    """Main NVDA thread: capture integers only, before opening any add-on UI."""
    import api
    return _capture(_native(), getattr(api.getForegroundObject(),'windowHandle',0),
                    getattr(api.getFocusObject(),'windowHandle',0))


def _check_context(native, context):
    if (native.root(native.foreground()) != context.frame
            or native.class_name(context.frame) != 'CabinetWClass'
            or native.active_tab(context.frame) != context.tab):
        raise ExplorerError('Okno lub karta Eksploratora zmieniły się. Naciśnij skrót ponownie.')


def get_current_folder(context):
    """Worker thread: own COM apartment and objects; return a plain Path."""
    native = _native()
    _check_context(native,context)
    path = _read_shell_path(context.frame,context.tab)
    _check_context(native,context)
    if not os.path.isdir(path):
        raise ExplorerError('To nie jest dostępny folder na dysku. Otwórz folder zawierający książki lub archiwa ZIP, RAR, 7z.')
    _check_context(native,context)
    # A filesystem check can block (notably on network drives) while the same
    # tab navigates elsewhere without changing either HWND.
    if _read_shell_path(context.frame,context.tab) != path:
        raise ExplorerError('Folder zmienił się podczas odczytu. Naciśnij skrót ponownie.')
    _check_context(native,context)
    return Path(path)


def get_selected_zip(context):
    """Read the selected ZIP/RAR/7z; legacy function name kept for callers."""
    return _get_selected(context)


def get_selected_source(context):
    """Worker: return one selected physical folder/archive, or None.

    Ordinary files and empty selection return None. Ambiguity, virtual paths,
    unavailable sources or changed Explorer context raise ExplorerError.
    """
    return _get_selected(context, include_folders=True)


def _get_selected(context, *, include_folders=False):
    native = _native()
    options = {'selection': True}
    if include_folders:
        options['include_folders'] = True
    _check_context(native, context)
    folder, path = _read_shell_path(context.frame, context.tab, **options)
    _check_context(native, context)
    if include_folders:
        if not os.path.isdir(folder):
            raise ExplorerError('To nie jest dostępny folder na dysku.')
        result = None
        if path is not None:
            if ntpath.splitext(path)[1].lower() in ('.zip', '.rar', '.7z') and os.path.isfile(path):
                result = Path(path)
            elif os.path.isdir(path):
                result = Path(path)
            elif not os.path.isfile(path):
                raise ExplorerError('Zaznaczone źródło nie jest dostępne na dysku.')
    else:
        if path is None:
            return None
        if not os.path.isfile(path):
            raise ExplorerError('Zaznaczone archiwum nie jest dostępnym plikiem na dysku.')
        result = Path(path)
    # Network filesystem checks can block while the user changes selection or
    # navigates within this same tab, keeping both HWNDs unchanged.
    _check_context(native, context)
    if _read_shell_path(context.frame, context.tab, **options) != (folder, path):
        raise ExplorerError('Folder lub zaznaczony plik zmienił się. Naciśnij skrót ponownie.')
    _check_context(native, context)
    return result


def _read_shell_path(frame, tab, *, selection=False, include_folders=False):
    # comtypes is provided by NVDA. Do not import it into Linux unit tests.
    import comtypes
    from comtypes import IUnknown, GUID, COMMETHOD, HRESULT, COMError
    from comtypes.client import CreateObject

    class IOleWindow(IUnknown):
        _iid_ = GUID('{00000114-0000-0000-C000-000000000046}')
        _methods_ = [
            COMMETHOD([],HRESULT,'GetWindow',(['out'],ctypes.POINTER(wintypes.HWND),'phwnd')),
            COMMETHOD([],HRESULT,'ContextSensitiveHelp',(['in'],wintypes.BOOL,'fEnterMode')),
        ]

    class IShellBrowser(IOleWindow):
        _iid_ = GUID('{000214E2-0000-0000-C000-000000000046}')
        # Prefix binding only. We use only inherited GetWindow, not further slots.
        _methods_ = []

    class IServiceProvider(IUnknown):
        _iid_ = GUID('{6D5140C1-7436-11CE-8034-00AA006009FA}')
        _methods_ = [COMMETHOD([],HRESULT,'QueryService',
            (['in'],ctypes.POINTER(GUID),'guidService'),
            (['in'],ctypes.POINTER(GUID),'riid'),
            (['out'],ctypes.POINTER(ctypes.POINTER(IUnknown)),'ppvObject'))]

    service = GUID('{4C96BE40-915C-11CF-99D3-00AA004AE837}')
    def tab_handle(window):
        provider = window.QueryInterface(IServiceProvider)
        unknown = provider.QueryService(ctypes.byref(service),ctypes.byref(IShellBrowser._iid_))
        return int(unknown.QueryInterface(IShellBrowser).GetWindow() or 0)

    initialized = False
    windows = shell = entries = None
    try:
        comtypes.CoInitialize()
        initialized = True
        shell = CreateObject('Shell.Application',dynamic=True)
        windows = shell.Windows()
        entries = [windows.Item(i) for i in range(windows.Count)]
        return _choose_folder(entries,frame,tab,tab_handle,selection=selection,
            include_folders=include_folders)
    except (ExplorerError,COMError,OSError,AttributeError,TypeError,ValueError) as error:
        message = (str(error) if isinstance(error,ExplorerError) else
            'Nie udało się odczytać folderu Eksploratora. Użyj Dodaj folder w bibliotece.')
        # Suspended frames (including chained COM failures) may own windows,
        # providers and items. Clear them while this apartment is still alive.
        pending = [error]
        while pending:
            caught = pending.pop()
            pending.extend(e for e in (caught.__cause__,caught.__context__) if e is not None)
            caught.__cause__ = caught.__context__ = None
            traceback.clear_frames(caught.__traceback__)
            caught.__traceback__ = None
        del caught
    finally:
        # Release apartment-owned references before CoUninitialize.
        entries = windows = shell = None
        if initialized:
            comtypes.CoUninitialize()
    # Do not marshal the original exception or any COM-owning traceback to GUI.
    raise ExplorerError(message)


class ExplorerError(RuntimeError):
    """A short user-facing explanation; never guess an ambiguous folder."""


def _choose_folder(windows, frame, tab, tab_handle, *, selection=False, include_folders=False):
    matches = [w for w in windows if int(w.HWND) == frame and tab_handle(w) == tab]
    if len(matches) != 1:
        raise ExplorerError('Nie udało się jednoznacznie rozpoznać aktywnej karty Eksploratora.')
    item = matches[0].Document.Folder.Self
    path = str(item.Path)
    if (not item.IsFileSystem or not ntpath.isabs(path)
            or (include_folders and (not ntpath.splitdrive(path)[0]
                or any(part in ('.', '..') for part in path.replace('/', '\\').split('\\'))))):
        raise ExplorerError('Otwórz zwykły folder z książkami, nie widok Ten komputer ani wyniki wyszukiwania.')
    selected_path = None
    if selection:
        selected = matches[0].Document.SelectedItems()
        count = int(selected.Count)
        if count > 1:
            if include_folders:
                raise ExplorerError('Zaznacz dokładnie jeden folder lub jeden plik ZIP, RAR lub 7z i naciśnij skrót ponownie.')
            raise ExplorerError('Zaznacz dokładnie jeden plik ZIP, RAR lub 7z i naciśnij skrót ponownie.')
        if count:
            selected_item = selected.Item(0)
            candidate = str(selected_item.Path)
            # Keep the raw selection (including ordinary files) for revalidation.
            # Shell IsFolder also describes ZIP files, so trust filesystem type.
            if include_folders or ntpath.splitext(candidate)[1].lower() in ('.zip', '.rar', '.7z'):
                if (not selected_item.IsFileSystem or not ntpath.isabs(candidate)
                        or not ntpath.splitdrive(candidate)[0]
                        or (include_folders and any(part in ('.', '..')
                            for part in candidate.replace('/', '\\').split('\\')))
                        or ntpath.normcase(ntpath.dirname(ntpath.normpath(candidate)))
                        != ntpath.normcase(ntpath.normpath(path))):
                    raise ExplorerError('Zaznacz plik ZIP, RAR lub 7z bezpośrednio w otwartym folderze na dysku.')
                selected_path = candidate
    if str(matches[0].Document.Folder.Self.Path) != path:
        raise ExplorerError('Folder zmienił się podczas odczytu. Naciśnij skrót ponownie.')
    return (path, selected_path) if selection else path
