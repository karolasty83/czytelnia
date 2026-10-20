"""Native wx controls; no NVDA imports, also testable outside the reader."""
import wx
from copy import deepcopy
from pathlib import Path

if __package__:
    from .numbering import track_label
else:
    # Standalone native dialog tests must not import the NVDA entry point.
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        'czytelnia_numbering', Path(__file__).with_name('numbering.py'))
    _numbering = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_numbering)
    track_label = _numbering.track_label


class ImportProgressDialog(wx.Dialog):
    """Modeless native progress. Hiding never cancels the import or plugin."""
    PHASES = {
        'prepare': 'Przygotowanie importu',
        'copy': 'Kopiowanie nagrań',
        'probe': 'Pomiar długości nagrań',
        'save': 'Zapisywanie biblioteki',
        'complete': 'Import zakończony',
    }

    def __init__(self, parent):
        super().__init__(parent, title='Import książki do biblioteki',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        outer = wx.BoxSizer(wx.VERTICAL)
        self.phase = wx.StaticText(self, label=self.PHASES['prepare'])
        outer.Add(self.phase, 0, wx.EXPAND | wx.ALL, 10)
        self.gauge = wx.Gauge(self, range=100, name='Postęp importu', size=(440, -1))
        outer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        description = wx.StaticText(self, label='Trwa import książki')
        outer.Add(description, 0, wx.ALL, 10)
        self.hideButton = wx.Button(self, label='Ukryj (Escape)')
        self.hideButton.Bind(wx.EVT_BUTTON, lambda event: self.Hide())
        outer.Add(self.hideButton, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        self.SetSizerAndFit(outer)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self._displayed = None
        self.update_progress('prepare', 0, 1)

    def update_progress(self, stage, done, total):
        # A count of completed work units, not a duration/ETA. Updating the
        # native gauge only on change gives NVDA its normal progressbar events.
        value = max(0, min(100 if stage == 'complete' else 99, int(100 * done / max(1, total))))
        label = f'{self.PHASES[stage]} — {value}%'
        if self._displayed == (stage, value):
            return
        self._displayed = stage, value
        self.phase.SetLabel(label)
        if self.gauge.GetValue() != value:
            self.gauge.SetValue(value)
        self.Layout()

    def on_close(self, event):
        if event.CanVeto():
            self.Hide()
            event.Veto()
        else:
            # Forced window destruction (e.g. system shutdown). The host checks
            # the wx object's validity before any later timer update/cleanup.
            event.Skip()

    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Hide()
        else:
            event.Skip()


class LibraryDialog(wx.Frame):
    """Modeless library with local shortcuts and dialog-style panel traversal."""
    def __init__(self, parent, host, books):
        super().__init__(parent, title='Czytelnia — biblioteka', size=(660, 440),
                         style=wx.DEFAULT_FRAME_STYLE)
        self.panel = wx.Panel(self, style=wx.TAB_TRAVERSAL)
        self.host = host
        self._window_id = self.GetId()
        self._adjustment = None
        self._adjustment_timer = None
        self.books = []
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self.panel, label='Książki w bibliotece:'), 0, wx.ALL, 8)
        self.bookList = wx.ListBox(self.panel, name='Książki w bibliotece')
        outer.Add(self.bookList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.status = wx.TextCtrl(self.panel, value='Wybierz książkę lub dodaj nową.',
                                  style=wx.TE_READONLY, name='Stan odtwarzania')
        outer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.playButton = self._button(row, 'Słuchaj', self.on_play, finish_adjustment=False)
        self.pauseButton = self._button(row, 'Pauza / wznów (Ctrl+P)', lambda e: host.action('toggle_selected'), finish_adjustment=False)
        self.bookButtons = [self.playButton, self.pauseButton]
        self.zipButton = self._button(row, 'Dodaj archiwum', lambda e: host.choose_zip())
        self._button(row, 'Dodaj folder', lambda e: host.choose_folder())
        self._button(row, 'Pomoc (F1)', lambda e: host.show_help())
        self._button(row, 'Zamknij (Escape)', self.on_close)
        outer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        self.panel.SetSizer(outer)
        self.playButton.SetDefault()
        self.bookList.Bind(wx.EVT_LISTBOX_DCLICK, self.on_play)
        self.bookList.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        for control in [self, self.panel] + list(self.panel.GetChildren()):
            control.Bind(wx.EVT_KEY_UP, self._on_key_up)
            control.Bind(wx.EVT_KILL_FOCUS, self._on_kill_focus)
        self._play_state_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._sync_play_button, self._play_state_timer)
        self.refresh(books)
        self.bookList.SetFocus()
        self._play_state_timer.Start(200)

    def _button(self, row, label, handler, *, finish_adjustment=True):
        button = wx.Button(self.panel, label=label)
        def invoke(event):
            self.stop_adjustment(finish=finish_adjustment)
            handler(event)
        button.Bind(wx.EVT_BUTTON, invoke)
        row.Add(button, 0, wx.ALL, 3)
        return button

    def selected(self):
        index = self.bookList.GetSelection()
        return self.books[index] if 0 <= index < len(self.books) else None

    def select_first(self):
        """Select the first book on an explicit open, never start playback."""
        if self.books:
            self.bookList.SetSelection(0)

    def refresh(self, books):
        previous = self.selected()
        old_index = self.bookList.GetSelection()
        self.books = list(books)
        self.bookList.Set([book['title'] for book in books])
        if books:
            index = next((i for i,b in enumerate(books) if previous and b['id'] == previous['id']),
                         max(0, min(old_index, len(books)-1)))
            self.bookList.SetSelection(index)
        self.playButton.Enable(bool(books))
        self.pauseButton.Enable(bool(books))
        book_controls = self.bookButtons
        if not books and self.FindFocus() in book_controls:
            self.zipButton.SetFocus()
        for button in book_controls:
            button.Show(bool(books))
        self._sync_play_button()
        self.panel.Layout()
        self.Layout()

    def _sync_play_button(self, event=None):
        if not self or self.IsBeingDeleted():
            return
        show = bool(self.books) and not self.host.is_playing()
        if self.playButton.IsShown() != show:
            if not show and self.FindFocus() is self.playButton:
                self.bookList.SetFocus()
            self.playButton.Show(show)
            self.panel.Layout()
            self.Layout()
        default = (self.playButton if show else self.pauseButton) if self.books else self.zipButton
        if self.GetDefaultItem() is not default:
            default.SetDefault()

    def on_play(self, event):
        book = self.selected()
        self.stop_adjustment(finish=book is None)
        if book:
            self.host.start_book(book['id'])

    def on_return(self, event):
        book = self.selected()
        if book:
            self.host.return_from_bookmark(book['id'])

    def on_remove(self, event):
        book = self.selected()
        if book:
            self.host.remove_book(book['id'])

    def on_context_menu(self, event):
        position = event.GetPosition()
        if position != wx.DefaultPosition:
            index = self.bookList.HitTest(self.bookList.ScreenToClient(position))
            if index == wx.NOT_FOUND:
                return
            self.bookList.SetSelection(index)
        book = self.selected()
        if not book:
            return
        book_id = book['id']
        self.stop_adjustment()
        self.bookList.SetFocus()
        menu = wx.Menu()
        try:
            for label, callback in (
                    ('Słuchaj', self.host.start_book),
                    ('Zakładki\tCtrl+B', self.host.show_bookmarks),
                    ('Wróć do słuchania\tCtrl+Shift+B', self.host.return_from_bookmark),
                    ('Przesunięcie numeracji odcinków…\tCtrl+N', self.host.show_numbering),
                    ('Usuń\tDelete', self.host.remove_book)):
                item = menu.Append(wx.ID_ANY, label)
                # Capture the ID now, not the selection after a worker refresh.
                menu.Bind(wx.EVT_MENU, lambda e, callback=callback: callback(book_id), item)
            self.bookList.PopupMenu(menu)
        finally:
            menu.Destroy()

    def _on_key_up(self, event):
        held = self._adjustment
        if held is not None and event.GetKeyCode() in (
                held['key'], wx.WXK_CONTROL, wx.WXK_SHIFT, wx.WXK_ALT,
                wx.WXK_WINDOWS_LEFT, wx.WXK_WINDOWS_RIGHT):
            self.stop_adjustment()
        event.Skip()

    def _on_kill_focus(self, event):
        self.stop_adjustment()
        event.Skip()

    def _on_activate(self, event):
        if not event.GetActive():
            self.stop_adjustment()
        event.Skip()

    def _on_destroy(self, event):
        # wxMSW may replace GetEventObject() with a base Window wrapper here;
        # the dialog's C++ object can already be deleted. Use the saved ID.
        if event.GetId() == self._window_id:
            self._play_state_timer.Stop()
            self.stop_adjustment()
        event.Skip()

    def stop_adjustment(self, *, finish=True):
        """End a keyboard hold and invalidate even an already queued tick."""
        held, self._adjustment = self._adjustment, None
        timer, self._adjustment_timer = self._adjustment_timer, None
        if timer is not None:
            timer.Stop()
        if held is not None and finish:
            self.host.end_adjustment()

    def _start_adjustment(self, event, name, delta):
        if event.IsAutoRepeat():
            return
        self.stop_adjustment()
        held = self._adjustment = {
            'key': event.GetKeyCode(), 'modifiers': event.GetModifiers(),
            'focus': self.FindFocus(), 'name': name, 'delta': delta,
        }
        if not self.host.adjust(name, delta):
            self.stop_adjustment()
            return
        if self._adjustment is held:
            self._adjustment_timer = wx.CallLater(100, self._repeat_adjustment, held)

    def _hold_is_pressed(self, held):
        if (not self or self.IsBeingDeleted() or not self.IsActive()
                or not held['focus'] or self.FindFocus() is not held['focus']):
            return False
        modifiers = wx.MOD_NONE
        for key, flag in ((wx.WXK_CONTROL, wx.MOD_CONTROL),
                          (wx.WXK_ALT, wx.MOD_ALT), (wx.WXK_SHIFT, wx.MOD_SHIFT)):
            if wx.GetKeyState(key):
                modifiers |= flag
        if wx.GetKeyState(wx.WXK_WINDOWS_LEFT) or wx.GetKeyState(wx.WXK_WINDOWS_RIGHT):
            modifiers |= wx.MOD_META
        return wx.GetKeyState(held['key']) and modifiers == held['modifiers']

    def _repeat_adjustment(self, held):
        if self._adjustment is not held:
            return
        if not self._hold_is_pressed(held):
            self.stop_adjustment()
            return
        # After navigation, retain release tracking only: a step while busy
        # would end the hold, or start a new gesture in the destination track.
        # Volume follows Windows autorepeat; its timer only watches the hold.
        if (not held.get('release_only') and held['name'] != 'change_volume'
                and not self.host.adjust(held['name'], held['delta'])):
            self.stop_adjustment()
            return
        if self._adjustment is held:
            self._adjustment_timer = wx.CallLater(100, self._repeat_adjustment, held)

    def on_key(self, event):
        focus = self.FindFocus()
        # Child modal windows own their keys, including Escape and editing
        # shortcuts. Do not let propagation reach playback in the parent.
        if not focus or wx.GetTopLevelParent(focus) is not self:
            event.Skip()
            return
        key = event.GetKeyCode()
        ctrl, alt, shift = event.ControlDown(), event.AltDown(), event.ShiftDown()
        if key == wx.WXK_F1 and not (ctrl or alt or shift or event.MetaDown()):
            if not event.IsAutoRepeat():
                self.stop_adjustment()
                self.host.show_help()
            return
        if ctrl and not (alt or shift or event.MetaDown()) and key == ord('N'):
            focus = self.FindFocus()
            if not focus or wx.GetTopLevelParent(focus) is not self:
                event.Skip()
                return
            if not event.IsAutoRepeat():
                book = self.selected()
                if book:
                    self.stop_adjustment()
                    self.host.show_numbering(book['id'])
            return
        if (focus is self.bookList and not (ctrl or alt or event.MetaDown())
                and ((key == wx.WXK_WINDOWS_MENU and not shift)
                     or (key == wx.WXK_F10 and shift))):
            if not event.IsAutoRepeat():
                context = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU)
                context.SetPosition(wx.DefaultPosition)
                self.on_context_menu(context)
            return
        held = self._adjustment
        if (held is not None and key == held['key']
                and event.GetModifiers() == held['modifiers']
                and self.FindFocus() is held['focus']):
            # Speed and seek use the fixed timer; volume uses native repeat.
            if held['name'] == 'change_volume' and event.IsAutoRepeat():
                if (not self._hold_is_pressed(held)
                        or (not held.get('release_only')
                            and not self.host.adjust(held['name'], held['delta']))):
                    self.stop_adjustment()
            return
        # These commands themselves finish the host gesture with resume
        # suppressed. Releasing it first lets the worker restart old audio
        # in the gap before the pause/start action arrives. Navigation instead
        # retains release tracking until the physical hold ends.
        plain_list = self.FindFocus() is self.bookList and not (ctrl or alt or shift or event.MetaDown())
        pause_key = (ctrl and not (alt or shift or event.MetaDown()) and key == ord('P'))
        if pause_key or (plain_list and key in (wx.WXK_SPACE, wx.WXK_RETURN)):
            self.stop_adjustment(finish=False)
            self.host.action('toggle_selected')
            return
        if alt and not (ctrl or shift or event.MetaDown()) and key in (wx.WXK_LEFT, wx.WXK_RIGHT):
            # Bounds are checked asynchronously; retain the release watchdog.
            if held is not None:
                held['release_only'] = True
            self.host.action('previous_track' if key == wx.WXK_LEFT else 'next_track')
            return
        if ctrl and shift and not (alt or event.MetaDown()) and key == ord('B'):
            focus = self.FindFocus()
            if not focus or wx.GetTopLevelParent(focus) is not self:
                event.Skip()
                return
            if not event.IsAutoRepeat():
                self.stop_adjustment(finish=False)
                self.on_return(event)
            return
        self.stop_adjustment()
        if key == wx.WXK_ESCAPE and not (ctrl or alt or shift):
            self.on_close(event)
        elif (ctrl and not (alt or shift or event.MetaDown())
              and key in (wx.WXK_LEFT, wx.WXK_RIGHT)):
            focus = self.FindFocus()
            if focus and wx.GetTopLevelParent(focus) is self:
                self._start_adjustment(event, 'seek_relative', -5 if key == wx.WXK_LEFT else 5)
            else:
                event.Skip()
        elif ctrl and not (alt or shift or event.MetaDown()) and key in (ord('B'), ord('D')):
            focus = self.FindFocus()
            if not focus or wx.GetTopLevelParent(focus) is not self:
                event.Skip()
                return
            if event.IsAutoRepeat():
                return
            if key == ord('D'):
                self.host.add_bookmark()
            else:
                book = self.selected()
                if book:
                    self.host.show_bookmarks(book['id'])
        elif ctrl and not (alt or event.MetaDown()) and key == ord('I'):
            self.host.action('book_status' if shift else 'status')
        elif (key in (ord('T'), ord('K')) and shift and not (alt or event.MetaDown())
              and (not ctrl or key == ord('K')) and self.FindFocus() is self.bookList):
            if event.IsAutoRepeat():
                return
            if key == ord('T'):
                self.host.sleep_timer_gesture()
            else:
                try:
                    if ctrl:
                        self.host.set_automatic_messages(not self.host.get_automatic_messages())
                    else:
                        self.host.set_auto_next(not self.host.get_auto_next())
                except Exception as error:
                    wx.MessageBox(str(error), 'Czytelnia', wx.OK | wx.ICON_ERROR, parent=self)
        elif (key in (wx.WXK_UP, wx.WXK_DOWN) and shift and not (alt or event.MetaDown())
              and self.FindFocus() is self.bookList and self.host.is_playing()):
            direction = 1 if key == wx.WXK_UP else -1
            self._start_adjustment(event, 'change_speed' if ctrl else 'change_volume',
                                   direction * (0.5 if ctrl else 1))
        elif plain_list and key == wx.WXK_DELETE:
            self.on_remove(event)
        else:
            event.Skip()

    def on_close(self, event):
        self._play_state_timer.Stop()
        self.stop_adjustment()
        self.host.dialog_closed(self)


class NumberingDialog(wx.Dialog):
    """One immutable worker snapshot; numbering changes no playback state."""
    def __init__(self, parent, book, track_index):
        super().__init__(parent, title='Przesunięcie numeracji odcinków',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.book = deepcopy(book)
        self.track_index = track_index
        file_label = f"Plik {track_index + 1} z {len(book['tracks'])}: {book['tracks'][track_index]['path']}"
        context = (f"Książka: {book['title']}\n{file_label}\n"
                   f"Obecna etykieta: {track_label(book, track_index)}\n"
                   'Ustawienie bieżącego odcinka jako pierwszego nada temu plikowi etykietę Odcinek 1. '
                   'Wcześniejsze pliki otrzymają oznaczenie „Wstęp”. Przywrócenie domyślnej numeracji '
                   'nada pierwszemu plikowi etykietę Odcinek 1.\n'
                   'Zmiana dotyczy tylko numeracji, nie miejsca ani stanu odtwarzania. '
                   'Wskazany plik nie zmieni się, nawet jeśli odtwarzanie przejdzie dalej.')
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label='Książka i bieżący odcinek:'), 0, wx.ALL, 8)
        self.context = wx.TextCtrl(self, value=context, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                   name='Książka i bieżący odcinek', size=(540, 150))
        outer.Add(self.context, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        action_label = f"Działanie — {book['title']} — {file_label} — {track_label(book, track_index)}"
        outer.Add(wx.StaticText(self, label=action_label), 0, wx.ALL, 8)
        self.action = wx.Choice(self, choices=['Ustaw bieżący odcinek jako pierwszy',
                                              'Przywróć domyślną numerację'], name=action_label)
        self.action.SetSelection(0)
        outer.Add(self.action, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        ok = wx.Button(self, wx.ID_OK, 'OK')
        ok.SetDefault()
        row.Add(ok, 0, wx.ALL, 4)
        row.Add(wx.Button(self, wx.ID_CANCEL, 'Anuluj'), 0, wx.ALL, 4)
        outer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.SetSizerAndFit(outer)
        self.SetEscapeId(wx.ID_CANCEL)
        self.action.SetFocus()

    def numbering_start(self):
        return self.track_index if self.action.GetSelection() == 0 else 0


class BookmarksDialog(wx.Dialog):
    """A snapshot of one book's bookmarks. Selection never controls playback."""
    def __init__(self, parent, host, book, bookmarks):
        super().__init__(parent, title='Zakładki — ' + book['title'], size=(620, 360),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.host, self.book_id = host, book['id']
        self.book = deepcopy(book)
        self._closed = False
        self.bookmarks = []
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label='Zakładki:'), 0, wx.ALL, 8)
        self.bookmarkList = wx.ListBox(self, name='Zakładki')
        outer.Add(self.bookmarkList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.renameButton = wx.Button(self, label='Zmień nazwę (F2)')
        self.removeButton = wx.Button(self, label='Usuń')
        self.closeButton = wx.Button(self, wx.ID_CANCEL, 'Zamknij')
        for button in (self.renameButton, self.removeButton, self.closeButton):
            row.Add(button, 0, wx.ALL, 4)
        outer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.SetSizer(outer)
        self.SetEscapeId(wx.ID_CANCEL)
        self.closeButton.Bind(wx.EVT_BUTTON, self.on_close)
        self.renameButton.Bind(wx.EVT_BUTTON, self.on_rename)
        self.removeButton.Bind(wx.EVT_BUTTON, self.on_remove)
        self.bookmarkList.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)
        self.bookmarkList.Bind(wx.EVT_LISTBOX_DCLICK, self.on_play)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.refresh(bookmarks)
        self.bookmarkList.SetFocus()

    def selected(self):
        index = self.bookmarkList.GetSelection()
        return self.bookmarks[index] if 0 <= index < len(self.bookmarks) else None

    def focus_bookmark(self, bookmark_id):
        for index, item in enumerate(self.bookmarks):
            if item['id'] == bookmark_id:
                self.bookmarkList.SetSelection(index)
                break
        self.bookmarkList.SetFocus()

    def refresh(self, bookmarks, selected_id=None):
        previous = self.selected()
        selected_id = selected_id or (previous['id'] if previous else None)
        old_index = self.bookmarkList.GetSelection()
        self.bookmarks = list(bookmarks)
        labels = []
        for item in self.bookmarks:
            seconds = max(0, int(item['position']))
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)
            labels.append(f"{item['name']} — {track_label(self.book, item['track_index']).lower()} — {hours}:{minutes:02}:{seconds:02}")
        self.bookmarkList.Set(labels)
        if self.bookmarks:
            index = next((i for i, b in enumerate(self.bookmarks) if b['id'] == selected_id),
                         max(0, min(old_index, len(self.bookmarks) - 1)))
            self.bookmarkList.SetSelection(index)
        self.renameButton.Enable(bool(bookmarks))
        self.removeButton.Enable(bool(bookmarks))

    def on_play(self, event):
        item = self.selected()
        if item:
            self.host.start_bookmark(self, item['id'])

    def on_rename(self, event):
        item = self.selected()
        if item:
            self.host.rename_bookmark(self, item['id'])

    def on_remove(self, event):
        item = self.selected()
        if item:
            self.host.remove_bookmark(self, item['id'])

    def on_context_menu(self, event):
        position = event.GetPosition()
        if position != wx.DefaultPosition:
            index = self.bookmarkList.HitTest(self.bookmarkList.ScreenToClient(position))
            if index == wx.NOT_FOUND:
                return
            self.bookmarkList.SetSelection(index)
        if self.selected() is None:
            return
        self.bookmarkList.SetFocus()
        menu = wx.Menu()
        try:
            rename = menu.Append(wx.ID_ANY, 'Zmień nazwę\tF2')
            remove = menu.Append(wx.ID_ANY, 'Usuń')
            menu.Bind(wx.EVT_MENU, self.on_rename, rename)
            menu.Bind(wx.EVT_MENU, self.on_remove, remove)
            self.bookmarkList.PopupMenu(menu)
        finally:
            menu.Destroy()

    def on_key(self, event):
        focus = self.FindFocus()
        if not focus or wx.GetTopLevelParent(focus) is not self:
            event.Skip()
            return
        if (event.ControlDown() and event.ShiftDown()
                and not (event.AltDown() or event.MetaDown())
                and event.GetKeyCode() == ord('B')):
            if not event.IsAutoRepeat():
                self.host.return_from_bookmark(self.book_id, self)
            return
        if not (event.ControlDown() or event.AltDown() or event.ShiftDown() or event.MetaDown()):
            if event.GetKeyCode() == wx.WXK_DELETE and focus is self.bookmarkList:
                if not event.IsAutoRepeat():
                    self.on_remove(event)
                return
            if event.GetKeyCode() == wx.WXK_F2:
                if not event.IsAutoRepeat():
                    self.on_rename(event)
                return
            if event.GetKeyCode() == wx.WXK_ESCAPE:
                self.on_close(event)
                return
            if event.GetKeyCode() == wx.WXK_RETURN and self.FindFocus() is self.bookmarkList:
                self.on_play(event)
                return
        event.Skip()

    def on_close(self, event=None):
        if self._closed:
            return
        self._closed = True
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)


class BookmarkNameDialog(wx.Dialog):
    def __init__(self, parent, current_name):
        super().__init__(parent, title='Zmień nazwę zakładki')
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label='Nazwa zakładki:'), 0, wx.ALL, 8)
        self.name = wx.TextCtrl(self, value=current_name, name='Nazwa zakładki', size=(360, -1))
        outer.Add(self.name, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        save = wx.Button(self, wx.ID_OK, 'Zapisz')
        save.SetDefault()
        save.Bind(wx.EVT_BUTTON, self.on_save)
        row.Add(save, 0, wx.ALL, 4)
        row.Add(wx.Button(self, wx.ID_CANCEL, 'Anuluj'), 0, wx.ALL, 4)
        outer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.SetSizerAndFit(outer)
        self.SetEscapeId(wx.ID_CANCEL)
        self.name.SetFocus()
        self.name.SelectAll()

    def bookmark_name(self):
        name = self.name.GetValue().strip()
        if not name:
            raise ValueError('Wpisz nazwę zakładki.')
        return name

    def on_save(self, event):
        try:
            self.bookmark_name()
        except ValueError as error:
            wx.MessageBox(str(error), 'Zmień nazwę zakładki', wx.OK | wx.ICON_ERROR, parent=self)
            self.name.SetFocus()
            return
        self.EndModal(wx.ID_OK)


class SleepTimerDialog(wx.Dialog):
    """Configure an inactive timer; scheduling and active state belong to the host.

    ``state`` is accepted only for constructor compatibility, never restored.
    """
    END_OF_PART = 'Po zakończeniu bieżącego odcinka'
    TRACK_COUNT = 'Po określonej liczbie odcinków'
    MINUTE_PRESETS = tuple(range(15, 121, 15))
    MAX_TRACKS = 10000
    ACTIONS = ('stop', 'sleep', 'shutdown')
    ACTION_LABELS = ('Zatrzymaj książkę', 'Przełącz komputer w stan uśpienia', 'Wyłącz komputer')

    def __init__(self, parent, state=None):
        super().__init__(parent, title='Wyłącznik czasowy',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label='Kiedy:'), 0, wx.ALL, 8)
        self.when = wx.Choice(self, choices=[self.END_OF_PART, self.TRACK_COUNT] +
                             [f'Za {minutes} minut' for minutes in self.MINUTE_PRESETS],
                             name='Kiedy')
        self.when.SetSelection(0)
        outer.Add(self.when, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.countLabel = wx.StaticText(self, label='Liczba odcinków:')
        outer.Add(self.countLabel, 0, wx.ALL, 8)
        self.count = wx.TextCtrl(self, value='', name='Liczba odcinków')
        outer.Add(self.count, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        outer.Add(wx.StaticText(self, label='Działanie:'), 0, wx.ALL, 8)
        self.action = wx.Choice(self, choices=list(self.ACTION_LABELS), name='Działanie')
        self.action.SetSelection(0)
        outer.Add(self.action, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.applyButton = wx.Button(self, wx.ID_OK, 'Zapisz')
        self.applyButton.Bind(wx.EVT_BUTTON, self.on_apply)
        self.applyButton.SetDefault()
        row.Add(self.applyButton, 0, wx.ALL, 4)
        row.Add(wx.Button(self, wx.ID_CANCEL, 'Anuluj'), 0, wx.ALL, 4)
        outer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.SetSizer(outer)
        self.when.Bind(wx.EVT_CHOICE, self.on_when_changed)
        self.on_when_changed()
        self.SetEscapeId(wx.ID_CANCEL)
        self.when.SetFocus()

    def on_when_changed(self, event=None):
        show = self.when.GetSelection() == 1
        self.countLabel.Show(show)
        self.count.Show(show)
        self.count.Enable(show)
        self.GetSizer().Fit(self)
        self.Layout()

    def config(self):
        """Return the requested timer configuration, or raise ValueError."""
        when = self.when.GetSelection()
        if not 0 <= when < 2 + len(self.MINUTE_PRESETS):
            raise ValueError('Wybierz, kiedy uruchomić działanie wyłącznika czasowego.')
        action = self.action.GetSelection()
        if not 0 <= action < len(self.ACTIONS):
            raise ValueError('Wybierz działanie wyłącznika czasowego.')
        config = {'minutes': self.MINUTE_PRESETS[when - 2] if when >= 2 else None,
                  'action': self.ACTIONS[action]}
        if when == 1:
            value = self.count.GetValue().strip()
            if not value:
                raise ValueError('Wpisz liczbę odcinków.')
            message = f'Wpisz liczbę odcinków od 1 do {self.MAX_TRACKS}.'
            if not value.isascii() or not value.isdecimal():
                raise ValueError(message)
            try:
                tracks = int(value)
            except ValueError:
                raise ValueError(message) from None
            if not 1 <= tracks <= self.MAX_TRACKS:
                raise ValueError(message)
            config['tracks'] = tracks
        return config

    def on_apply(self, event):
        try:
            self.config()
        except ValueError as error:
            wx.MessageBox(str(error), 'Wyłącznik czasowy', wx.OK | wx.ICON_ERROR, parent=self)
            if self.when.GetSelection() == wx.NOT_FOUND:
                self.when.SetFocus()
            elif self.action.GetSelection() == wx.NOT_FOUND:
                self.action.SetFocus()
            else:
                self.count.SetFocus()
            return
        self.EndModal(wx.ID_OK)


class CandidateDialog(wx.Dialog):
    def __init__(self, parent, paths, source=None):
        super().__init__(parent, title='Dodaj książkę z bieżącego folderu', size=(620, 360),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.paths = list(paths)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label='Folder źródłowy:'), 0, wx.ALL, 8)
        self.sourceCtrl = wx.TextCtrl(self, value=str(source) if source is not None else '',
                                     style=wx.TE_READONLY, name='Folder źródłowy')
        outer.Add(self.sourceCtrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        outer.Add(wx.StaticText(self, label='Książki do dodania:'), 0, wx.ALL, 8)
        formats = {'.zip': 'ZIP', '.rar': 'RAR', '.7z': '7z'}
        labels = [p.name + ' — ' + formats.get(p.suffix.lower(), 'folder') for p in paths]
        self.candidates = wx.ListBox(self, choices=labels, name='Książki do dodania')
        outer.Add(self.candidates, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.addButton = wx.Button(self, wx.ID_OK, 'Dodaj do biblioteki')
        self.addButton.SetDefault()
        self.addButton.Enable(bool(paths))
        row.Add(self.addButton, 0, wx.ALL, 4)
        row.Add(wx.Button(self, wx.ID_CANCEL, 'Anuluj'), 0, wx.ALL, 4)
        outer.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.SetSizer(outer)
        self.SetEscapeId(wx.ID_CANCEL)
        self.candidates.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self.EndModal(wx.ID_OK))
        if paths: self.candidates.SetSelection(0)
        self.candidates.SetFocus()

    def selected_path(self):
        index = self.candidates.GetSelection()
        return self.paths[index] if 0 <= index < len(self.paths) else None
