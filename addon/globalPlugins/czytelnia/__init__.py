"""Czytelnia: one global gesture, managed library, serialized background I/O."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import threading
import time
import traceback

import config
import core
import globalPluginHandler
import globalVars
import gui
from logHandler import log
import scriptHandler
import speech
import ui
import wx

from .error_log import ErrorLog
from .library import Library, FolderSelectionRequired, scan_candidates
from .player import Player, probe_duration
from .controller import Controller
from .dialogs import LibraryDialog, CandidateDialog, SleepTimerDialog, ImportProgressDialog
from .preferences import Preferences
from .sleep_timer import SleepTimer
from . import power
from . import dialogs as bookmark_dialogs
from .explorer import capture_context, get_current_folder, get_selected_source


class _ImportProgress:
    """One latest-value mailbox; the worker never touches a wx object."""
    def __init__(self, previous):
        self._lock = threading.Lock()
        self._latest = ('prepare', 0, 1)
        self.previous = previous
        self.window = None
        self.timer = None

    def report(self, stage, done, total):
        with self._lock:
            self._latest = (stage, done, total)

    def snapshot(self):
        with self._lock:
            return self._latest


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = 'Czytelnia'

    def __init__(self):
        super().__init__()
        self._closed = False
        self._error_log = None
        self._error_log_lock = threading.Lock()
        self._power_lock = threading.RLock()
        self._dialog = None
        self._numbering_request = None
        self._numbering_dialog = None
        self._numbering_parent_to_destroy = None
        self._bookmark_request = None
        self._bookmark_dialog = None
        self._bookmark_edit_token = None
        self._bookmark_prompt = None
        self._bookmark_closing = False
        self._bookmark_parent_to_destroy = None
        self._import_progress = None
        self._press_timer = None
        self._sleep_key_timer = None
        self._sleep_key_token = None
        self._sleep_key_deadline = 0
        self._sleep_key_block_until = 0
        self._sleep_key_was_active = False
        self._sleep_dialog_token = None
        self._sleep_dialog_open = False
        self._save_timer = None
        self._transition_timers = set()
        self._sleep_poll_timer = None
        self._timer_poll_pending = False
        self._preferences_error = None
        try:
            self._preferences = Preferences(Path(globalVars.appArgs.configPath) / "addons" / "czytelnia-ustawienia.json")
        except (OSError, ValueError) as error:
            self._preferences = None
            self._preferences_error = str(error)
            log.error("Czytelnia: odczyt ustawień", exc_info=True)
        self._controller = None
        self._busy = False
        self._adjust_session = None
        self._adjust_sessions = {}  # at most one latest target per adjustment kind
        self._adjust_running = None
        self._adjust_inflight = False
        self._adjust_interrupted = False
        self._adjust_announced = float('-inf')
        self._adjust_last_message = None
        self._save_pending = False
        self._cancel = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='Czytelnia')
        self._duration_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='Czytelnia-duration')
        self._book_status_pending = False  # UI-owned gate, including snapshot and delivery.
        self._schedule_save()

    def _initialize(self):
        if self._controller is None:
            addons = Path(globalVars.appArgs.configPath) / 'addons'
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data or not Path(local_app_data).is_absolute():
                raise ValueError("Nie można ustalić katalogu LOCALAPPDATA dla biblioteki.")
            binaries = Path(__file__).resolve().parents[2] / 'tools'
            library = Library(
                addons, media_dir=Path(local_app_data) / "Czytelnia" / "biblioteka",
                duration_probe=lambda path: probe_duration(path, binaries / 'ffprobe.exe'),
                sevenzip_path=binaries / '7z.exe')
            self._controller = Controller(
                library,
                lambda callback: Player(binaries / 'ffplay.exe', binaries / 'ffprobe.exe',
                                        on_event=callback, diagnostic=self._write_diagnostic),
                self._dispatch_event, self._say, transition=self._transition,
                auto_next=self._get_saved_auto_next, sleep_timer=SleepTimer(),
                power=self._perform_power, cancelled=lambda: self._closed,
                notify_auto=self._say_auto, preferences=self._preferences)
        return self._controller

    def _write_diagnostic(self, stage, detail):
        # Lazy creation: successful playback never creates a log file.
        # No logging configuration changes in the shared NVDA process.
        try:
            with self._error_log_lock:
                if self._error_log is None:
                    folder = os.environ.get('LOCALAPPDATA')
                    if not folder or not Path(folder).is_absolute():
                        return False
                    self._error_log = ErrorLog(Path(folder) / 'Czytelnia' / 'biblioteka')
                writer = self._error_log
            return writer.write(stage, detail)
        except Exception:
            return False

    def _record_exception(self, stage):
        try:
            self._write_diagnostic(stage, traceback.format_exc())
        except Exception:
            pass

    def _perform_power(self, action):
        # Pair the final check/system call with terminate's cancellation signal.
        # Neither audio shutdown nor progress saving holds this lock.
        with self._power_lock:
            if not self._closed:
                power.perform(action)

    def is_playing(self):
        if self._closed or self._controller is None:
            return False
        return self._controller.player.is_playing or any(
            s.resume and s.allow_resume and not s.cancelled and not s.done
            and s.context == self._controller.adjustment_context
            for s in self._adjust_sessions.values())

    def _get_saved_auto_next(self):
        return self._preferences.auto_next if self._preferences is not None else True

    def get_auto_next(self):
        if self._controller is not None:
            return self._controller.effective_auto_next()
        return self._get_saved_auto_next()

    def set_auto_next(self, value):
        if self._preferences is None:
            raise ValueError('Nie można zapisać ustawień. ' + str(self._preferences_error))
        self._preferences.set_auto_next(value)
        if self._controller:
            self._dispatch_event(lambda: self._controller.auto_next_changed(value))
        self._say('Przechodzenie do następnego odcinka ' + ('włączone.' if value else 'wyłączone.'))

    def get_automatic_messages(self):
        return self._preferences.automatic_messages if self._preferences is not None else True

    def set_automatic_messages(self, value):
        if self._preferences is None:
            raise ValueError('Nie można zapisać ustawień. ' + str(self._preferences_error))
        self._preferences.set_automatic_messages(value)
        self._say('Komunikaty automatyczne ' + ('włączone.' if value else 'wyłączone.'))

    def cancel_sleep_timer_gesture(self):
        self._sleep_dialog_token = None
        timer, self._sleep_key_timer = self._sleep_key_timer, None
        self._sleep_key_token = None
        if timer:
            timer.Stop()

    def sleep_timer_gesture(self):
        """UI-thread multi-press; never queue cancellation behind slow book I/O."""
        if self._closed or self._sleep_dialog_open:
            return
        now = time.monotonic()
        if now < self._sleep_key_block_until:
            return
        timeout = max(0.1, int(config.conf['keyboard']['multiPressTimeout']) / 1000)
        if self._sleep_key_timer and now <= self._sleep_key_deadline:
            was_active = self._sleep_key_was_active
            self.cancel_sleep_timer_gesture()
            self._sleep_key_block_until = now + timeout
            controller = self._controller
            if was_active:
                speech.cancelSpeech()
            if controller and controller.timer_state():
                controller.cancel_timer()  # Only the timer's short lock, no player or disk.
                self._sleep_state_updated(None)
            elif was_active:
                # poll() may have consumed the timer while pause/save is still
                # pending. Cancellation must invalidate that action's epoch too.
                if controller:
                    controller.cancel_timer()
                    self._sleep_state_updated(None)
                self._say('Wyłącznik czasowy nieaktywny.')
            else:
                self.show_sleep_timer()
            return
        self.cancel_sleep_timer_gesture()
        self._sleep_key_was_active = bool(self._controller and self._controller.timer_state())
        self._sleep_key_deadline = now + timeout
        token = self._sleep_key_token = object()
        def single():
            if self._closed or self._sleep_key_token is not token:
                return
            self._sleep_key_timer = None
            self._sleep_key_token = None
            if not self._sleep_key_was_active:
                self.show_sleep_timer()
        self._sleep_key_timer = core.callLater(int(timeout * 1000), single)
        if self._sleep_key_was_active:
            # Already on the UI thread: speak now; the timer only keeps the
            # double-press window open and never repeats the status.
            self._deliver_message(self._controller.timer_status())

    def show_sleep_timer(self):
        if self._closed or self._sleep_dialog_open or self._sleep_dialog_token is not None:
            return
        self.cancel_sleep_timer_gesture()
        controller = self._controller
        if controller and controller.timer_state():
            self._say(controller.timer_status())
        elif not self._busy:
            # core.callLater dispatches inside NVDA's CorePump. A modal must
            # begin in a separate wx event, after that pump has returned.
            token = self._sleep_dialog_token = object()
            wx.CallAfter(self._open_sleep_timer, token)
        else:
            self._say('Trwa poprzednia operacja. Poczekaj na jej zakończenie.')

    def _open_sleep_timer(self, token):
        if self._closed or self._sleep_dialog_token is not token:
            return
        self._sleep_dialog_token = None
        controller = self._controller
        if controller and controller.timer_state():
            self._say(controller.timer_status())
            return
        if self._busy:
            self._say('Trwa poprzednia operacja. Poczekaj na jej zakończenie.')
            return
        self._sleep_dialog_open = True
        try:
            self._show_sleep_timer(None)
        finally:
            self._sleep_dialog_open = False

    def _show_sleep_timer(self, state):
        dialog = SleepTimerDialog(self._dialog or gui.mainFrame, state=state)
        try:
            answer = self._modal(dialog)
            selected = dialog.config() if answer == wx.ID_OK else None
        finally:
            dialog.Destroy()
        if selected is None or self._closed:
            return
        action = selected['action']
        if action in ('sleep', 'shutdown'):
            operation = 'przełączyć komputer w stan uśpienia' if action == 'sleep' else 'wyłączyć komputer'
            if selected.get('tracks') is not None:
                when = 'po zakończeniu {0} {1}'.format(
                    selected['tracks'], 'odcinka' if selected['tracks'] == 1 else 'odcinków')
            else:
                when = ('po zakończeniu bieżącego odcinka' if selected['minutes'] is None
                        else 'za {0} minut'.format(selected['minutes']))
            confirmation = wx.MessageDialog(self._dialog or gui.mainFrame,
                'Czy {0} {1}? Zapisz pracę w innych programach. Książka zostanie zatrzymana, a miejsce zapisane.'.format(operation, when),
                'Wyłącznik czasowy', style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
            try:
                confirmation.SetYesNoLabels('&Tak', '&Nie')
                accepted = self._modal(confirmation) == wx.ID_YES
            finally:
                confirmation.Destroy()
            if not accepted or self._closed:
                return
        def arm(controller):
            controller.arm_timer(**selected)
            return controller.timer_state()
        self._run(arm, self._sleep_state_updated)

    def _sleep_state_updated(self, state):
        if self._sleep_poll_timer:
            self._sleep_poll_timer.Stop()
            self._sleep_poll_timer = None
        if not self._closed and state and state['mode'] == 'minutes':
            self._sleep_poll_timer = wx.CallLater(1000, self._poll_sleep_timer)

    def _poll_sleep_timer(self):
        self._sleep_poll_timer = None
        if self._closed or self._controller is None:
            return
        if self._busy or self._timer_poll_pending:
            self._sleep_poll_timer = wx.CallLater(1000, self._poll_sleep_timer)
            return
        self._timer_poll_pending = True
        def work():
            state = None
            try:
                if not self._closed:
                    self._controller.check_timer()
                    state = self._controller.timer_state()
            except Exception as error:
                log.error('Czytelnia: wyłącznik czasowy', exc_info=True)
                self._say('Nie udało się wykonać działania wyłącznika. ' + str(error))
            finally:
                self._timer_poll_pending = False
                wx.CallAfter(self._sleep_state_updated, state)
        self._executor.submit(work)

    def _say(self, text):
        wx.CallAfter(self._deliver_message, str(text))

    def _say_auto(self, text):
        wx.CallAfter(self._deliver_automatic_message, str(text))

    def _deliver_automatic_message(self, text):
        if self.get_automatic_messages():
            self._deliver_message(text)

    def _deliver_message(self, text):
        if not self._closed:
            ui.message(text)
            if self._dialog:
                self._dialog.status.ChangeValue(text)

    def _transition(self, message, is_current, resume):
        # Controller calls from the worker. All timers/UI stay on the wx thread.
        wx.CallAfter(self._begin_transition, message, is_current, resume)

    def _transition_after(self, callback):
        def fire():
            self._transition_timers.discard(timer)
            if not self._closed:
                callback()
        timer = wx.CallLater(1000, fire)
        self._transition_timers.add(timer)

    def _begin_transition(self, message, is_current, resume):
        if self._closed or not is_current():
            return
        def finish():
            if is_current():
                self._dispatch_event(resume)
        def announce():
            if not is_current():
                return
            self._deliver_automatic_message(message)
            # Preserve the transition gaps even when automatic speech is off.
            self._transition_after(finish)
        self._transition_after(announce)

    def _dispatch_event(self, fn):
        if not self._closed:
            try:
                self._executor.submit(self._event_work, fn)
            except RuntimeError:
                pass  # Executor already closed during plugin termination.

    def _event_work(self, fn):
        if self._closed:
            return
        try:
            fn()
        except Exception:
            log.error('Czytelnia: obsługa zdarzenia odtwarzacza', exc_info=True)
            self._say('Nie udało się przejść do kolejnej części lub zapisać postępu. Szczegóły w dzienniku NVDA.')

    def adjust(self, name, delta):
        """UI-side step: one job, at most one latest target per control kind."""
        if self._closed or self._busy or self._controller is None or self._adjust_interrupted:
            return False
        session = self._adjust_session
        if session is not None and (session.cancelled
                or session.context != self._controller.adjustment_context):
            session.cancel()
            self._adjust_session = None
            self._adjust_interrupted = True
            return False
        if session is None or session.name != name or session.done:
            self.end_adjustment()
            session = self._adjust_sessions.get(name)
            if (session is None or session.done or session.cancelled
                    or session.context != self._controller.adjustment_context):
                session = self._controller.new_adjustment(name)
            if session is None:
                return False
            self._adjust_sessions[name] = session
            self._adjust_session = session
            self._adjust_last_message = None
            self._adjust_announced = float('-inf')
        if not session.move(delta):
            return False
        self._queue_adjustment()
        return True

    def end_adjustment(self):
        self._adjust_interrupted = False
        if self._adjust_session is not None:
            session = self._adjust_session
            session.release()
            if session.cancelled or session.context != self._controller.adjustment_context:
                session.cancel()
                if self._adjust_sessions.get(session.name) is session:
                    del self._adjust_sessions[session.name]
                self._adjust_session = None
            self._queue_adjustment()

    def _queue_adjustment(self):
        if self._closed or self._busy or self._adjust_inflight:
            return
        session = next((s for s in self._adjust_sessions.values()
                        if not s.done and not s.cancelled
                        and s.applied_revision != s.snapshot()[2]), None)
        if session is None:
            return
        self._adjust_inflight = True
        self._adjust_running = session
        def work():
            message = error = None
            try:
                if not self._closed:
                    message = self._controller.apply_adjustment(session)
            except Exception as caught:
                error = str(caught)
                session.cancel()
                self._controller.cancel_adjustment()
                log.error('Czytelnia: regulacja odtwarzania', exc_info=True)
            finally:
                wx.CallAfter(self._adjust_done, session, message, error)
        self._executor.submit(work)

    def _adjust_done(self, session, message, error):
        self._adjust_inflight = False
        self._adjust_running = None
        if self._closed:
            return
        if error:
            self._deliver_message('Nie udało się zmienić odtwarzania. ' + error)
        if session is self._adjust_session:
            now = time.monotonic()
            latest = session.applied_revision == session.snapshot()[2]
            if (message and latest and not session.cancelled
                    and message != self._adjust_last_message
                    and (session.done or now - self._adjust_announced >= .5)):
                self._deliver_message(message)
                self._adjust_announced, self._adjust_last_message = now, message
            if session.done or session.cancelled:
                self._adjust_interrupted = session.cancelled and not session.released
                self._adjust_session = None
        if session.done or session.cancelled:
            if self._adjust_sessions.get(session.name) is session:
                del self._adjust_sessions[session.name]
        self._queue_adjustment()

    def _navigate(self, name):
        """Check bounds on the worker before touching a held UI gesture."""
        if self._closed:
            return
        if self._busy:
            self._say('Trwa poprzednia operacja. Poczekaj na jej zakończenie.')
            return
        adjustments = list(self._adjust_sessions.values())
        self._busy = True

        def work():
            consumed = []
            active = []
            error = None
            try:
                if self._closed:
                    return
                controller = self._initialize()
                # get_book can wait for import's library lock: never on wx.
                book = (controller.library.get_book(controller.book_id)
                        if controller.book_id is not None else None)
                index = controller.track_index + (1 if name == 'next_track' else -1)
                if self._closed:
                    return
                resume = None
                if book is not None and 0 <= index < len(book['tracks']):
                    # A held speed/seek may temporarily stop the backend without
                    # representing the user's pause. Capture intent before
                    # finalizing those sessions with their old-track resume off.
                    active = [s for s in adjustments
                              if not s.cancelled and not s.done
                              and s.context == controller.adjustment_context]
                    resume = controller.player.is_playing or any(
                        s.resume and s.allow_resume for s in active)
                    consumed = adjustments
                    # Suppress every resume before applying any kind: applying
                    # a different kind may finish the previous held session.
                    for adjustment in consumed:
                        adjustment.allow_resume = False
                        adjustment.release()
                    for adjustment in consumed:
                        controller.apply_adjustment(adjustment)
                        if self._closed:
                            return
                # A due timer can cancel the held session while it is settled.
                # Navigation must not undo that stop with the captured intent.
                if any(s.cancelled for s in active):
                    resume = False
                # The controller owns boundary/no-book messages. No fallback
                # play, timer poll, save or gesture finish on the no-op path.
                getattr(controller, name)(resume=resume)
            except Exception as caught:
                error = caught
                self._record_exception('operation')
                log.error('Czytelnia: operacja nie powiodła się', exc_info=True)
            finally:
                wx.CallAfter(self._navigation_done, consumed, error)

        self._executor.submit(work)

    def _navigation_done(self, consumed, error):
        # UI-owned references are removed only after valid navigation consumed
        # them. A release during the job stays queued for boundary completion.
        for session in consumed:
            if self._adjust_sessions.get(session.name) is session:
                del self._adjust_sessions[session.name]
            if self._adjust_session is session:
                self._adjust_session = None
        self._done(None, None, error)
        self._queue_adjustment()

    def _run(self, fn, success=None, *, resume_adjustment=True, failure=None):
        if self._closed:
            return False
        if self._busy:
            self._say('Trwa poprzednia operacja. Poczekaj na jej zakończenie.')
            return False
        adjustments = list(self._adjust_sessions.values())
        for adjustment in adjustments:
            if not resume_adjustment:
                adjustment.allow_resume = False
            adjustment.release()
        self.end_adjustment()
        self._adjust_session = None
        self._adjust_sessions.clear()
        self._busy = True
        def work():
            try:
                if self._closed:
                    return
                controller = self._initialize()
                for adjustment in adjustments:
                    controller.apply_adjustment(adjustment)
                result = fn(controller)
                wx.CallAfter(self._done, success, result, None)
            except Exception as error:
                self._record_exception('operation')
                log.error('Czytelnia: operacja nie powiodła się', exc_info=True)
                wx.CallAfter(self._done, None, None, error, failure)
        try:
            self._executor.submit(work)
        except Exception as error:
            log.error('Czytelnia: uruchamianie operacji', exc_info=True)
            self._done(None, None, error, failure)
            return False
        return True

    def _done(self, callback, result, error, failure=None):
        self._busy = False
        if self._closed:
            return
        try:
            if error:
                if failure:
                    failure()
                self._deliver_message('Nie udało się wykonać operacji. {0}'.format(error))
            elif callback:
                callback(result)
        except Exception:
            log.error('Czytelnia: wyświetlanie wyniku', exc_info=True)
            self._deliver_message('Nie udało się wyświetlić wyniku. Szczegóły w dzienniku NVDA.')

    def _schedule_save(self):
        if not self._closed:
            self._save_timer = core.callLater(10000, self._autosave)

    def _autosave(self):
        if self._closed:
            return
        if self._controller and not self._save_pending and not self._busy:
            self._save_pending = True
            def save():
                try:
                    if not self._closed:
                        self._controller.save()
                except Exception:
                    log.error('Czytelnia: automatyczny zapis postępu', exc_info=True)
                    self._say('Nie udało się zapisać miejsca słuchania. Sprawdź wolne miejsce i uprawnienia.')
                finally:
                    self._save_pending = False
            self._executor.submit(save)
        self._schedule_save()

    @scriptHandler.script(description='Otwiera bibliotekę audiobooków. Dwukrotnie: potwierdzenie dodania zaznaczonego archiwum ZIP, RAR lub 7z albo lista książek z folderu Eksploratora.')
    def script_czytelnia(self, gesture):
        if self._closed:
            return
        repeat = scriptHandler.getLastScriptRepeatCount()
        if repeat == 0:
            if self._press_timer:
                self._press_timer.Stop()
            timeout = int(config.conf['keyboard']['multiPressTimeout'])
            self._press_timer = core.callLater(timeout + 50, self._single_press)
        elif repeat == 1:
            if self._press_timer:
                self._press_timer.Stop()
                self._press_timer = None
            self.scan_explorer()

    def _single_press(self):
        self._press_timer = None
        if not self._closed:
            self.show_library()

    def show_help(self):
        if self._closed:
            return
        try:
            import addonHandler
            document = addonHandler.getCodeAddon().getDocFilePath()
            if not document or not Path(document).is_file():
                raise FileNotFoundError('Brak pliku podręcznika w dodatku.')
            os.startfile(document)
        except Exception as error:
            log.error('Czytelnia: otwieranie podręcznika', exc_info=True)
            self._say(f'Nie można otworzyć podręcznika: {error}')

    def show_library(self):
        if self._closed:
            return
        if self._import_progress is not None:
            window = self._import_progress.window
            if window:
                window.Show()
                window.Raise()
                window.hideButton.SetFocus()
            else:
                self._say('Trwa import książki. Biblioteka otworzy się po zakończeniu.')
            return
        if self._dialog:
            if self._dialog.bookList.GetCount():
                self._dialog.bookList.SetSelection(0)
            self._dialog.Raise()
            self._dialog.bookList.SetFocus()
            return
        self._run(lambda c: c.library.books(), self._show_library)

    def _show_library(self, books):
        if self._dialog:
            self._dialog.refresh(books)
            self._dialog.Show()
            self._dialog.Raise()
            return
        gui.mainFrame.prePopup()
        try:
            self._dialog = LibraryDialog(gui.mainFrame, self, books)
            self._dialog.Show()
            self._dialog.bookList.SetFocus()
        finally:
            gui.mainFrame.postPopup()
        if not books:
            self._say('Biblioteka jest pusta. Wybierz Dodaj archiwum lub Dodaj folder.')

    def dialog_closed(self, dialog):
        self._close_numbering()
        self._close_bookmarks()
        self.cancel_sleep_timer_gesture()
        self.end_adjustment()
        if dialog is self._dialog:
            self._dialog = None
        if self._numbering_dialog:
            self._numbering_parent_to_destroy = dialog
        elif self._bookmark_dialog:
            self._bookmark_parent_to_destroy = dialog
        else:
            dialog.Destroy()
        # Closing the library does not stop the book.

    @staticmethod
    def _destroy_window(window):
        if window and not window.IsBeingDeleted():
            window.Destroy()

    def _modal(self, dialog):
        gui.mainFrame.prePopup()
        try:
            return dialog.ShowModal()
        finally:
            gui.mainFrame.postPopup()

    def choose_zip(self):
        dialog = wx.FileDialog(self._dialog, 'Wybierz archiwum książki',
                               wildcard='Archiwa książek (*.zip;*.rar;*.7z)|*.zip;*.rar;*.7z',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if self._modal(dialog) == wx.ID_OK:
                source = Path(dialog.GetPath())
            else:
                return
        finally:
            dialog.Destroy()
        self.import_source(source)

    def choose_folder(self):
        dialog = wx.DirDialog(self._dialog, 'Wybierz folder książki', style=wx.DD_DIR_MUST_EXIST)
        try:
            if self._modal(dialog) == wx.ID_OK:
                source = Path(dialog.GetPath())
            else:
                return
        finally:
            dialog.Destroy()
        self.import_source(source)

    def scan_explorer(self):
        try:
            context = capture_context()
        except Exception as error:
            log.error('Czytelnia: kontekst Eksploratora', exc_info=True)
            self._say(str(error))
            return
        def scan(controller):
            selected = get_selected_source(context)
            if selected is not None and not selected.is_dir():
                return 'zip', selected
            folder = selected if selected is not None else get_current_folder(context)
            try:
                paths = scan_candidates(folder)
                if folder in paths:
                    result = 'folder', folder
                elif selected is not None:
                    result = 'message', 'Nie znaleziono plików dźwiękowych w źródle.'
                else:
                    result = 'list', (folder, paths)
            except FolderSelectionRequired as error:
                result = 'message', str(error)
            # Scanning a network folder can block while selection or navigation
            # changes. Revalidate even when the result is only a spoken message.
            if (get_selected_source(context) != selected
                    or (selected is None and get_current_folder(context) != folder)):
                raise RuntimeError('Folder lub zaznaczenie zmieniły się podczas szukania książek. Naciśnij skrót ponownie.')
            return result
        self._run(scan, self._explorer_result)

    def _explorer_result(self, result):
        kind, value = result
        if kind == 'zip':
            self._confirm_import(value)
        elif kind == 'folder':
            self._confirm_import(value, is_folder=True)
        elif kind == 'message':
            self._say(value)
        else:
            self._show_candidates(value)

    def _confirm_import(self, source, *, is_folder=False):
        dialog = wx.MessageDialog(
            self._dialog or gui.mainFrame,
            'Czy chcesz dodać do biblioteki: {0}?'.format(source.name if is_folder else source.stem),
            'Dodawanie książek do biblioteki',
            style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            dialog.SetYesNoLabels('&Tak', '&Nie')
            answer = self._modal(dialog)
        finally:
            dialog.Destroy()
        if answer == wx.ID_YES and not self._closed:
            self.import_source(source)

    def _show_candidates(self, result):
        folder, paths = result
        if not paths:
            self._say('W tym folderze nie znaleziono archiwów ZIP, RAR, 7z ani folderów z nagraniami.')
            return
        dialog = CandidateDialog(self._dialog or gui.mainFrame, paths, source=folder)
        try:
            if self._modal(dialog) == wx.ID_OK:
                source = dialog.selected_path()
            else:
                return
        finally:
            dialog.Destroy()
        if source:
            self.import_source(source)

    def import_source(self, source):
        if self._closed:
            return
        if self._busy or self._import_progress is not None:
            self._say('Trwa poprzednia operacja.')
            return
        state = self._import_progress = _ImportProgress(self._dialog)
        try:
            gui.mainFrame.prePopup()
            try:
                state.window = ImportProgressDialog(gui.mainFrame)
                if self._dialog:
                    self._dialog.Hide()
                state.window.Show()
                state.window.Raise()
                state.window.hideButton.SetFocus()
            finally:
                gui.mainFrame.postPopup()
            self._poll_import_progress(state)
        except Exception as error:
            log.error('Czytelnia: okno importu', exc_info=True)
            self._import_failed(state)
            self._deliver_message('Nie udało się otworzyć okna importu. ' + str(error))
            return
        def work(controller):
            book = controller.library.import_book(
                source, stage_progress=state.report, cancel=self._cancel)
            return book, controller.library.books()
        if not self._run(work, lambda result: self._import_done(state, result),
                         failure=lambda: self._import_failed(state)):
            self._import_failed(state)

    def _poll_import_progress(self, state):
        if self._closed or self._import_progress is not state:
            return
        if state.window:
            state.window.update_progress(*state.snapshot())
        state.timer = wx.CallLater(200, self._poll_import_progress, state)

    def _close_import_progress(self, state):
        if self._import_progress is not state:
            return
        self._import_progress = None
        if state.timer:
            state.timer.Stop()
            state.timer = None
        if state.window:
            state.window.Destroy()
        state.window = None

    def _import_failed(self, state):
        if self._closed or self._import_progress is not state:
            return
        self._close_import_progress(state)
        if self._dialog and self._dialog is state.previous:
            self._dialog.Show()
            self._dialog.Raise()
            self._dialog.bookList.SetFocus()

    def _import_done(self, state, result):
        if self._closed or self._import_progress is not state:
            return
        self._close_import_progress(state)
        book, books = result
        self._show_library(books)
        for index, item in enumerate(books):
            if item['id'] == book['id']:
                self._dialog.bookList.SetSelection(index)
                break
        self._dialog.bookList.SetFocus()
        self._say('Książka jest w bibliotece: {0}. Części: {1}. Wybierz Słuchaj.'.format(book['title'], len(book['tracks'])))

    def show_numbering(self, book_id):
        parent = self._dialog
        if (self._closed or not parent or self._numbering_request is not None
                or self._numbering_dialog or self._bookmark_dialog):
            return
        token = self._numbering_request = object()
        def read(controller):
            book = controller.library.get_book(book_id)
            index = (controller.track_index if controller.book_id == book_id
                     else book['track_index'] if book else None)
            return book, index
        def failed():
            if self._numbering_request is token:
                self._numbering_request = None
        if not self._run(read, lambda result: wx.CallAfter(
                self._open_numbering, token, parent, book_id, result), failure=failed):
            failed()

    def _focus_numbering_book(self, parent, book_id):
        if not self._closed and parent and parent is self._dialog and not parent.IsBeingDeleted():
            for index, book in enumerate(parent.books):
                if book['id'] == book_id:
                    parent.bookList.SetSelection(index)
                    break
            parent.bookList.SetFocus()

    def _open_numbering(self, token, parent, book_id, result):
        if self._closed or self._numbering_request is not token:
            return
        writing = False
        try:
            if not parent or parent is not self._dialog or parent.IsBeingDeleted() or self._busy:
                return
            book, index = result
            selected = parent.selected()
            if not book or not selected or selected['id'] != book_id:
                return
            dialog = self._numbering_dialog = bookmark_dialogs.NumberingDialog(parent, book, index)
            try:
                answer = self._modal(dialog)
                start = dialog.numbering_start() if answer == wx.ID_OK else None
            finally:
                self._numbering_dialog = None
                self._destroy_window(dialog)
                pending, self._numbering_parent_to_destroy = self._numbering_parent_to_destroy, None
                self._destroy_window(pending)
                self._focus_numbering_book(parent, book_id)
            if (start is None or self._closed or self._numbering_request is not token
                    or parent is not self._dialog):
                return
            def write(controller):
                controller.library.set_numbering_start(book_id, start)
                return controller.library.get_book(book_id)
            def done(updated):
                if self._numbering_request is not token:
                    return
                self._numbering_request = None
                if parent and parent is self._dialog and not parent.IsBeingDeleted():
                    parent.refresh([updated if b['id'] == book_id else b for b in parent.books])
                    self._focus_numbering_book(parent, book_id)
                self._say('Zmieniono numerację odcinków książki: ' + book['title'] + '.')
            def failed():
                if self._numbering_request is token:
                    self._numbering_request = None
                    self._focus_numbering_book(parent, book_id)
            writing = self._run(write, done, failure=failed)
        finally:
            if not writing and self._numbering_request is token:
                self._numbering_request = None

    def _close_numbering(self):
        self._numbering_request = None
        if self._numbering_dialog and self._numbering_dialog.IsModal():
            self._numbering_dialog.EndModal(wx.ID_CANCEL)

    def add_bookmark(self):
        # The controller, not the library selection, owns the listening position.
        self._run(lambda c: c.add_bookmark())

    def show_bookmarks(self, book_id):
        parent = self._dialog
        if self._closed or not parent or self._bookmark_request is not None or self._bookmark_dialog:
            return
        token = self._bookmark_request = object()
        def read(controller):
            book = controller.library.get_book(book_id)
            return book, controller.library.bookmarks(book_id) if book else []
        def failed():
            if self._bookmark_request is token:
                self._bookmark_request = None
        if not self._run(read, lambda result: wx.CallAfter(
                self._open_bookmarks, token, parent, book_id, result), failure=failed):
            failed()

    def _open_bookmarks(self, token, parent, book_id, result):
        if self._closed or self._bookmark_request is not token:
            return
        self._bookmark_request = None
        if not parent or parent is not self._dialog or parent.IsBeingDeleted() or self._busy:
            return
        selected = parent.selected()
        book, rows = result
        if not book or not selected or selected['id'] != book_id:
            return
        if not rows:
            self._say('brak zakładek')
            return
        self._bookmark_closing = False
        dialog = self._bookmark_dialog = bookmark_dialogs.BookmarksDialog(parent, self, book, rows)
        try:
            self._modal(dialog)
        finally:
            self._bookmark_dialog = None
            if dialog:
                dialog.Destroy()
            pending, self._bookmark_parent_to_destroy = self._bookmark_parent_to_destroy, None
            self._destroy_window(pending)
            if not self._closed and parent and parent is self._dialog and not parent.IsBeingDeleted():
                parent.bookList.SetFocus()

    def _bookmarks_live(self, dialog):
        return (not self._closed and not self._bookmark_closing
                and bool(dialog) and dialog is self._bookmark_dialog
                and not dialog._closed and not dialog.IsBeingDeleted()
                and bool(self._dialog) and dialog.GetParent() is self._dialog
                and not self._dialog.IsBeingDeleted())

    def _close_bookmarks(self):
        self._bookmark_request = None
        self._bookmark_edit_token = None
        if self._bookmark_closing:
            return
        self._bookmark_closing = True
        if self._bookmark_prompt and self._bookmark_prompt.IsModal():
            # Only the innermost wxMSW modal loop is running. The prompt's
            # finally closes the outer list AFTER ShowModal has unwound.
            self._bookmark_prompt.EndModal(wx.ID_CANCEL)
            return
        if self._bookmark_dialog:
            self._bookmark_dialog.on_close()

    def start_bookmark(self, dialog, bookmark_id):
        if not self._bookmarks_live(dialog) or self._bookmark_edit_token is not None:
            return
        book_id = dialog.book_id
        def done(result):
            if self._bookmarks_live(dialog):
                dialog.on_close()
        self._run(lambda c: c.start_bookmark(book_id, bookmark_id), done, resume_adjustment=False)

    def return_from_bookmark(self, book_id, dialog=None):
        if self._bookmark_edit_token is not None:
            return
        if dialog is not None and not self._bookmarks_live(dialog):
            return
        def done(restored):
            if restored and dialog is not None and self._bookmarks_live(dialog):
                dialog.on_close()
        self._run(lambda c: c.return_from_bookmark(book_id), done, resume_adjustment=False)

    def rename_bookmark(self, dialog, bookmark_id):
        self._edit_bookmark(dialog, bookmark_id, remove=False)

    def remove_bookmark(self, dialog, bookmark_id):
        self._edit_bookmark(dialog, bookmark_id, remove=True)

    def _edit_bookmark(self, dialog, bookmark_id, *, remove):
        if not self._bookmarks_live(dialog) or self._bookmark_edit_token is not None:
            return
        token = self._bookmark_edit_token = object()
        book_id = dialog.book_id
        def failed():
            if self._bookmark_edit_token is token:
                self._bookmark_edit_token = None
        if not self._run(lambda c: c.library.get_bookmark(book_id, bookmark_id),
                lambda item: wx.CallAfter(self._prompt_bookmark_edit, token, dialog, item, remove),
                failure=failed):
            failed()

    def _prompt_bookmark_edit(self, token, dialog, item, remove):
        if self._bookmark_edit_token is not token:
            return
        try:
            if not self._bookmarks_live(dialog) or self._busy:
                return
            if not item:
                self._say('Nie znaleziono zakładki.')
                return
            book_id, bookmark_id = dialog.book_id, item['id']
            if remove:
                prompt = wx.MessageDialog(dialog, 'Usunąć zakładkę „{0}”?'.format(item['name']),
                    'Usuń zakładkę', style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
                prompt.SetYesNoLabels('&Tak', '&Nie')
            else:
                prompt = bookmark_dialogs.BookmarkNameDialog(dialog, item['name'])
            self._bookmark_prompt = prompt
            try:
                answer = self._modal(prompt)
                accepted = answer == (wx.ID_YES if remove else wx.ID_OK)
                name = prompt.bookmark_name() if accepted and not remove else None
            finally:
                self._bookmark_prompt = None
                if prompt:
                    prompt.Destroy()
                if self._bookmark_closing and dialog and not dialog.IsBeingDeleted():
                    dialog.on_close()
            if not self._bookmarks_live(dialog):
                return
            dialog.focus_bookmark(bookmark_id)
            if not accepted:
                return
            def write(controller):
                if remove:
                    controller.library.remove_bookmark(book_id, bookmark_id)
                else:
                    controller.library.rename_bookmark(book_id, bookmark_id, name)
                return controller.library.bookmarks(book_id)
            self._run(write, lambda rows: self._bookmarks_updated(dialog, rows, bookmark_id))
        finally:
            if self._bookmark_edit_token is token:
                self._bookmark_edit_token = None

    def _bookmarks_updated(self, dialog, rows, selected_id):
        if not self._bookmarks_live(dialog):
            return
        if not rows:
            dialog.on_close()
            self._say('brak zakładek')
        else:
            dialog.refresh(rows, selected_id=selected_id)
            dialog.bookmarkList.SetFocus()

    def start_book(self, book_id):
        self._run(lambda c: c.start(book_id), resume_adjustment=False)

    def action(self, name, *args):
        if name not in {'toggle','toggle_selected','seek_relative','previous_track','next_track','status','book_status','change_volume','change_speed'}:
            return
        if name in ('seek_relative', 'change_volume', 'change_speed'):
            if self.adjust(name, *args):
                self.end_adjustment()
            return
        if name == 'book_status':
            self._request_book_status()
            return
        if name in ('previous_track', 'next_track'):
            self._navigate(name)
            return
        if name in ('toggle', 'toggle_selected'):
            # Read wx selection NOW on the UI thread, not later in the worker.
            selected = self._dialog.selected() if self._dialog else None
            args = (selected['id'] if selected else None,)
            if name == 'toggle_selected' and selected is None:
                return
        was_adjusting = any(s.resume and not s.cancelled and not s.done
                            for s in self._adjust_sessions.values())
        force_pause = name in ('toggle', 'toggle_selected') and was_adjusting
        def work(controller):
            if name == 'toggle_selected':
                if controller.book_id != args[0]:
                    controller.start(args[0])
                else:
                    controller.toggle(*args, force_pause=force_pause)
                return
            if force_pause:
                result = controller.toggle(*args, force_pause=True)
            else:
                result = getattr(controller,name)(*args)
            if name == 'status':
                self._say(result)
        self._run(work, resume_adjustment=name == 'status')

    def _request_book_status(self):
        """One bounded scan, independent of playback operations and timer polls."""
        if self._closed or self._book_status_pending:
            return
        self._book_status_pending = True

        def prepare():
            snapshot = None
            try:
                controller = self._initialize()
                snapshot = controller.book_status_snapshot()
                def complete(future):
                    text = error = None
                    try:
                        text = controller.complete_book_status(snapshot, future.result())
                    except Exception as caught:
                        error = str(caught)
                        log.error('Czytelnia: długość książki', exc_info=True)
                    wx.CallAfter(self._book_status_done, snapshot, text, error)
                future = self._duration_executor.submit(
                    controller.probe_book_status, snapshot, self._cancel.is_set)
                future.add_done_callback(lambda done: self._dispatch_event(lambda: complete(done)))
            except Exception as caught:
                log.error('Czytelnia: długość książki', exc_info=True)
                wx.CallAfter(self._book_status_done, snapshot, None, str(caught))
        self._dispatch_event(prepare)

    def _book_status_done(self, snapshot, text, error):
        # UI thread: re-check the book token, including changes after rendering
        # but before wx delivered this callback. No wx widgets on either worker.
        self._book_status_pending = False
        if self._closed or (snapshot is not None and snapshot['token'].is_set()):
            return
        if error:
            self._deliver_message('Nie udało się ustalić długości książki. ' + error)
        elif text is not None:
            self._deliver_message(text)

    def remove_book(self, book_id):
        if self._busy:
            self._say('Zaczekaj na zakończenie poprzedniej operacji.')
            return
        answer = gui.messageBox(
            'Usunąć kopię książki z biblioteki wraz z postępem i zakładkami?',
            'Usuń książkę', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, parent=self._dialog)
        if answer != wx.YES:
            return
        def work(controller):
            controller.remove(book_id)
            return controller.library.books()
        def done(books):
            if self._bookmark_dialog and self._bookmark_dialog.book_id == book_id:
                self._close_bookmarks()
            if self._dialog:
                self._dialog.refresh(books)
                self._dialog.bookList.SetFocus()
            self._say('Usunięto kopię książki z biblioteki.')
        current = self._controller and self._controller.book_id == book_id
        self._run(work, done, resume_adjustment=not current)

    def terminate(self):
        with self._power_lock:
            if self._closed:
                return
            self._closed = True
        self._cancel.set()
        self._close_numbering()
        self._close_bookmarks()
        if self._import_progress is not None:
            self._close_import_progress(self._import_progress)
        self._duration_executor.shutdown(wait=False, cancel_futures=True)
        self.cancel_sleep_timer_gesture()
        for session in (*self._adjust_sessions.values(), self._adjust_session, self._adjust_running):
            if session is not None:
                session.cancel()
        self._adjust_session = None
        self._adjust_sessions.clear()
        if self._sleep_poll_timer:
            self._sleep_poll_timer.Stop()
            self._sleep_poll_timer = None
        for timer in self._transition_timers:
            timer.Stop()
        self._transition_timers.clear()
        for timer in (self._press_timer,self._save_timer):
            if timer:
                timer.Stop()
        if self._dialog:
            dialog, self._dialog = self._dialog, None
            if self._numbering_dialog:
                self._numbering_parent_to_destroy = dialog
            elif self._bookmark_dialog:
                self._bookmark_parent_to_destroy = dialog
            else:
                dialog.Destroy()
        def finish():
            try:
                if self._controller:
                    self._controller.close()
            except Exception:
                log.error('Czytelnia: zamykanie odtwarzacza / zapis postępu', exc_info=True)
        self._executor.submit(finish)
        self._executor.shutdown(wait=False)
        super().terminate()

    __gestures = {'kb:NVDA+shift+c':'czytelnia'}


if globalVars.appArgs.secure:
    GlobalPlugin = globalPluginHandler.GlobalPlugin
