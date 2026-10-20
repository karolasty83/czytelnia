"""Book/track coordination. Public methods run on one serialized worker."""
import threading
from pathlib import Path

if __package__:
    from .time_text import format_time
    from .numbering import track_label
else:
    # Standalone tools/tests must not import the NVDA plugin entry point.
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        'czytelnia_time_text', Path(__file__).with_name('time_text.py'))
    _time_text = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_time_text)
    format_time = _time_text.format_time
    _spec = importlib.util.spec_from_file_location(
        'czytelnia_numbering', Path(__file__).with_name('numbering.py'))
    _numbering = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_numbering)
    track_label = _numbering.track_label


class Adjustment:
    """One bounded latest target, shared by the UI and serialized worker.

    The lock protects small value copies only, never audio or filesystem I/O.
    """
    def __init__(self, name, context, value, low, high, resume):
        self.name, self.context = name, context
        self.target, self.low, self.high = value, low, high
        self.resume = resume
        self.allow_resume = True
        self.revision = 0
        self.applied_revision = -1
        self.released = False
        self.done = False
        self.cancelled = False
        self.started = False
        self._lock = threading.Lock()

    def move(self, delta):
        with self._lock:
            if self.done or self.cancelled:
                return False
            self.target = max(self.low, min(self.high, self.target + delta))
            self.released = False
            self.revision += 1
            return True

    def release(self):
        with self._lock:
            if not self.released and not self.done:
                self.released = True
                self.revision += 1

    def cancel(self):
        with self._lock:
            self.cancelled = True

    def snapshot(self):
        with self._lock:
            return self.target, self.released, self.revision

    def seal(self, revision):
        with self._lock:
            if self.cancelled or self.done or revision != self.revision or not self.released:
                return False
            self.done = True
            return True


class Controller:
    def __init__(self, library, player_factory, dispatch, notify, transition=None, auto_next=None, sleep_timer=None, power=None, cancelled=None, notify_auto=None, preferences=None):
        self.library = library
        self.cancelled = cancelled if cancelled is not None else lambda: False
        self._power_lock = threading.RLock()
        self._timer_lock = threading.RLock()
        self._timer_epoch = 0
        self._book_status_token = threading.Event()
        self.dispatch = dispatch
        self.notify = notify
        self.notify_auto = notify_auto if notify_auto is not None else self.notify
        self.transition = transition
        self.sleep_timer = sleep_timer
        self.power = power
        self.auto_next = auto_next if auto_next is not None else lambda: True
        self._pending_transition = None
        self._adjustment = None
        self._adjust_epoch = 0
        self.book_id = None
        self.track_index = 0
        self.generation = 0
        self.closed = False
        self._end_lock = threading.Lock()
        self._pending_end = None
        self._claimed_end = None
        self.player = player_factory(self._player_event)
        self.preferences = preferences
        if preferences is not None:
            self.player.set_volume(preferences.volume)
            self.player.set_speed(preferences.speed)

    def _save_playback_setting(self, name):
        if self.preferences is None:
            return
        try:
            getattr(self.preferences, 'set_' + name)(getattr(self.player, name))
        except (OSError, ValueError) as error:
            # A disk failure must not strand a completed speed gesture in pause.
            self.notify('Nie udało się zapisać ustawień odtwarzania: {0}'.format(error))

    def _player_event(self, event, details):
        with self._end_lock:
            generation = self.generation
            session = self._adjustment
            held_live_seek = bool(session and session.name == 'seek_relative'
                                  and not session.done and not session.cancelled
                                  and getattr(self.player, 'supports_live_seek', False))
            if event == 'ended' and generation != self._claimed_end:
                self._pending_end = generation
        # Release/save can finish before dispatch runs: retain arrival context.
        self.dispatch(lambda: self._handle_event(generation, event, details, held_live_seek))

    def _claim_natural_end(self, generation):
        # Only the backend's EOF is evidence: seeking to duration is not EOF.
        # Never hold this lock over player operations (pause drains callbacks).
        with self._end_lock:
            if self._pending_end != generation or generation == self._claimed_end:
                return False
            self._pending_end = None
            self._claimed_end = generation
            return True

    def _handle_event(self, generation, event, details, held_live_seek=False):
        if self.closed or generation != self.generation or self.book_id is None:
            return
        if event in ('error', 'ended'):
            self.cancel_adjustment()
        if event == 'error':
            self._pending_transition = None
            self.save()
            self.notify('Błąd odtwarzania: {0}'.format(details))
        elif event == 'ended':
            if self._pending_transition is not None:
                return
            if not self._claim_natural_end(generation):
                return
            if self.check_timer(ended=True):
                return
            if held_live_seek:
                self.player.pause()
                self.generation += 1
                self.save()
                return
            book = self.library.get_book(self.book_id)
            if not self.effective_auto_next() and self.track_index + 1 < len(book['tracks']):
                self.player.pause()
                self.generation += 1
                self.library.save_position(self.book_id, self.track_index, self.player.duration)
                self.notify_auto('Koniec {0}.'.format(track_label(book, self.track_index)
                    .replace('Odcinek', 'odcinka').replace('Wstęp', 'wstępu')))
            elif self.track_index + 1 < len(book['tracks']):
                self._advance_after_end(book)
            else:
                self.player.pause()
                self.generation += 1
                self.library.save_position(self.book_id, self.track_index, self.player.duration)
                self.notify_auto('Koniec książki.')

    def effective_auto_next(self):
        """Stored preference plus the current book's transient count timer."""
        if self.auto_next():
            return True
        state = self.timer_state()
        return bool(state and state['mode'] == 'tracks'
                    and self.book_id is not None and state['target'][0] == self.book_id)

    def auto_next_changed(self, value):
        if not value:
            state = self.timer_state()
            if state and state['mode'] == 'tracks':
                self.cancel_timer()
            if self._pending_transition is not None:
                self._pending_transition = None
                self.generation += 1

    def _advance_after_end(self, book):
        # Pause is a callback barrier: old audio must retain its old generation.
        self.player.pause()
        self.save()
        self.generation += 1
        track = self.track_index + 1
        path = self.library.track_path(self.book_id, track)
        duration = self.library.track_duration(self.book_id, track)
        self.player.load(path, position=0, duration=duration)
        self.track_index = track
        self.save()
        generation = self.generation
        self._pending_transition = generation

        def is_current():
            return (not self.closed and self.generation == generation
                    and self._pending_transition == generation and self.effective_auto_next())

        def resume():
            # The orchestrator dispatches this onto the serialized worker.
            if is_current():
                self._pending_transition = None
                self.generation += 1
                self.player.play()

        message = track_label(book, track) + '.'
        if self.transition is None:
            resume()  # Standalone legacy callers have no UI timer orchestrator.
        else:
            try:
                self.transition(message, is_current, resume)
            except Exception:
                self._pending_transition = None
                raise

    @property
    def adjustment_context(self):
        return self.book_id, self.track_index, self._adjust_epoch

    def new_adjustment(self, name):
        """UI-side snapshot: no disk/process operations, only brief clock locks."""
        context = self.adjustment_context
        if self.closed or self.cancelled() or self.book_id is None:
            return None
        prior = self._adjustment
        resume = self.player.is_playing or bool(
            prior and prior.context == context and not prior.cancelled and prior.resume)
        if name == 'seek_relative':
            value, low, high = self.player.position, 0, self.player.duration
        elif name == 'change_volume' and resume:
            value, low, high = self.player.volume, 0, 100
        elif name == 'change_speed' and resume:
            value, low, high = self.player.speed, 1.0, 2.0
        else:
            return None
        if context != self.adjustment_context:
            return None
        return Adjustment(name, context, value, low, high, resume)

    def cancel_adjustment(self):
        """Invalidate queued gestures at playback/timer boundaries. No resume."""
        self._adjust_epoch += 1
        if self._adjustment:
            self._adjustment.cancel()
            self._adjustment = None

    def finish_adjustment(self):
        session = self._adjustment
        if session is not None:
            session.release()
            return self.apply_adjustment(session)

    def apply_adjustment(self, session):
        """Worker-side apply of the latest value, not a queue of key presses."""
        if session.done:
            return None
        if (session.cancelled or self.closed or self.cancelled()
                or session.context != self.adjustment_context):
            session.cancel()
            return None
        if self.check_timer():
            session.cancel()
            return None
        if self._adjustment is not None and self._adjustment is not session:
            self.finish_adjustment()
        if session.context != self.adjustment_context or self.cancelled():
            session.cancel()
            return None
        value, released, revision = session.snapshot()
        if (not session.started and session.name == 'change_speed'
                and value == self.player.speed):
            # Clamping at a bound is a real no-op: do not pause, invalidate EOF,
            # save progress or announce an unchanged setting. A later reversal
            # can still change this unstarted gesture normally.
            session.applied_revision = revision
            if released:
                session.seal(revision)
            return None
        self._adjustment = session
        if session.name == 'seek_relative' and getattr(self.player, 'supports_live_seek', False):
            return self._apply_live_seek(session)
        if not session.started:
            session.started = True
            if session.name in ('seek_relative', 'change_speed'):
                self._pending_transition = None
                self.player.pause()
                if session.name == 'change_speed' and self.player.position >= self.player.duration:
                    # Natural EOF won the pause barrier: do not swallow its
                    # next-track/end-timer handling by invalidating generation.
                    self.player.set_speed(session.snapshot()[0])
                    # EOF completes this gesture even before the usual release path.
                    self._save_playback_setting('speed')
                    if self.closed or self.cancelled():
                        return None
                    self._handle_event(self.generation, 'ended', None)
                    return None
                # Old EOF callbacks retain the old controller generation.
                self.generation += 1
        # Pause/previous work can be slow: read the target AFTER that barrier.
        value, released, revision = session.snapshot()
        if session.cancelled or self.cancelled():
            return None
        message = None
        if session.name == 'seek_relative':
            if self.player.position != value:
                self.player.seek(value)
        elif session.name == 'change_volume':
            if self.player.volume != value:
                self.player.set_volume(value)
            message = 'Głośność {0} procent.'.format(value)
        elif session.name == 'change_speed':
            if self.player.speed != value:
                self.player.set_speed(value)
            message = 'Prędkość {0}×'.format(format(value, 'g').replace('.', ','))
        session.applied_revision = revision
        if released:
            if session.name != 'change_volume':
                self.save()
            # No timer, shutdown or superseded target may restart old audio.
            if self.check_timer() or self.cancelled() or session.cancelled:
                return None
            if session.seal(revision):
                if session.name in ('change_volume', 'change_speed'):
                    self._save_playback_setting(session.name.removeprefix('change_'))
                # Disk I/O can outlive termination or cancellation of this gesture.
                if self.closed or self.cancelled() or session.cancelled:
                    return None
                if (session.name != 'change_volume' and session.resume and session.allow_resume
                        and self.player.position < self.player.duration):
                    self.player.play()
                if self._adjustment is session:
                    self._adjustment = None
        return message

    def _seek_live(self, seconds):
        """Hand off EOF ownership inside the live backend's seek barrier.

        supports_live_seek requires seek(seconds, *, before_seek=None): the
        backend drains old callbacks, then calls before_seek under its
        callback/operation barrier before committing the new position. New
        callbacks may arrive before seek returns, so never advance afterward.
        """
        def before_seek():
            with self._end_lock:
                self.generation += 1

        # Never hold _end_lock while entering/draining the backend.
        self.player.seek(seconds, before_seek=before_seek)

    def _apply_live_seek(self, session):
        """Keep audio running; release saves the actual clock, not the old target."""
        value, released, revision = session.snapshot()
        self._pending_transition = None
        try:
            if not session.started or value != getattr(session, '_last_seek', None):
                self._seek_live(value)
                session.started = True
                session._last_seek = value
                if self.check_timer():
                    return None
                if (session.resume and session.allow_resume and not self.player.is_playing
                        and self.player.position < self.player.duration
                        and not session.cancelled and not self.cancelled()):
                    self.player.play()
            session.applied_revision = revision
            if released:
                self.save()
                if self.check_timer() or self.cancelled() or session.cancelled:
                    return None
                if session.seal(revision) and self._adjustment is session:
                    self._adjustment = None
        except Exception:
            session.cancel()
            self._fail_live_seek()
            raise
        return None

    def _fail_live_seek(self):
        """Quarantine failed playback without replacing the original error."""
        self.cancel_adjustment()
        self._pending_transition = None
        try:
            self.player.pause()  # Drain callbacks with the failed generation.
        except Exception:
            pass  # Cleanup failure must not hide the seek/save exception.
        finally:
            # UI snapshots captured while pause was blocked must also expire.
            self.cancel_adjustment()
            self.generation += 1

    def seek_relative(self, seconds):
        self.cancel_adjustment()
        self._pending_transition = None
        if self.book_id is None:
            return
        target = max(0, min(self.player.duration, self.player.position + seconds))
        if getattr(self.player, 'supports_live_seek', False):
            try:
                self._seek_live(target)
                self.save()
            except Exception:
                self._fail_live_seek()
                raise
            return
        was_playing = self.player.is_playing
        # Drain/invalidate old callbacks while they still see the old generation.
        self.player.pause()
        self.generation += 1
        self.player.seek(target)
        if was_playing and self.player.position < self.player.duration:
            self.player.play()
        self.save()

    def arm_timer(self, minutes=None, action='stop', tracks=None):
        if self.sleep_timer is None or self.closed:
            raise RuntimeError('Wyłącznik czasowy jest niedostępny.')
        if minutes is None and self.book_id is None:
            raise ValueError('Najpierw uruchom książkę, aby wybrać koniec bieżącej części.')
        if tracks is not None:
            if type(tracks) is not int or tracks <= 0:
                raise ValueError('Liczba odcinków musi być dodatnią liczbą całkowitą.')
            if minutes is not None:
                raise ValueError('Wybierz minuty albo liczbę odcinków, nie oba tryby.')
            available = len(self.library.get_book(self.book_id)['tracks']) - self.track_index
            if tracks > available:
                raise ValueError('Liczba odcinków przekracza liczbę pozostałych odcinków książki: {0}.'.format(available))
        target = (self.book_id, self.track_index) if minutes is None else None
        options = dict(minutes=minutes, action=action, target=target)
        if tracks is not None:
            options['tracks'] = tracks
        with self._timer_lock:
            self.sleep_timer.arm(**options)
            self._timer_epoch += 1
        message = 'Wyłącznik czasowy włączony.'
        if tracks is not None and not self.auto_next():
            message += (' Automatyczne przechodzenie do następnego odcinka '
                        'włączone tymczasowo na czas wyłącznika.')
        self.notify(message)

    def timer_state(self):
        return self.sleep_timer.snapshot() if self.sleep_timer is not None else None

    def timer_status(self):
        """UI-safe: one in-memory timer snapshot, never player or disk I/O."""
        state = self.timer_state()
        if state is None:
            return 'Wyłącznik czasowy nieaktywny.'
        action = {'stop': 'zatrzymanie odtwarzania',
                  'shutdown': 'wyłączenie komputera',
                  'sleep': 'przełączenie w stan uśpienia'}[state['action']]
        if state['mode'] == 'minutes':
            when = 'Za ' + format_time(state['remaining'], accusative=True)
        elif state['mode'] == 'tracks':
            count = state['remaining_tracks']
            when = 'Po zakończeniu {0} {1}'.format(count, 'odcinka' if count == 1 else 'odcinków')
        else:
            when = 'Po zakończeniu bieżącego odcinka'
        return 'Wyłącznik czasowy aktywny. {0} {1}.'.format(when, action)

    def cancel_timer(self):
        # Also invalidate a consumed action blocked in pause/save on the worker.
        # This lock is never held over player, filesystem or system calls.
        with self._timer_lock:
            self._timer_epoch += 1
            active = self.sleep_timer is not None and self.sleep_timer.cancel()
        if active:
            self.notify('Wyłącznik czasowy anulowany.')

    def _cancel_end_timer(self):
        state = self.timer_state()
        if state and state['mode'] in ('end', 'tracks'):
            self.cancel_timer()

    def check_timer(self, ended=False):
        if self.closed or self.cancelled() or self.sleep_timer is None:
            return False
        with self._timer_lock:
            epoch = self._timer_epoch
            action = self.sleep_timer.poll(target=(self.book_id, self.track_index), ended=ended)
        if action is None:
            return False
        # Consume once, stop audio and commit progress BEFORE any system action.
        self.cancel_adjustment()
        self._pending_transition = None
        self.player.pause()
        self.generation += 1
        self.save()
        if action == 'stop':
            self.notify_auto('Wyłącznik czasowy: zatrzymano książkę.')
        else:
            # Never hold this lock across pause/save: termination must be able
            # to cancel while either is blocked. Only power is committed here.
            with self._power_lock:
                with self._timer_lock:
                    if self.closed or self.cancelled() or epoch != self._timer_epoch:
                        return True
                if self.power is None:
                    raise RuntimeError('Działanie systemowe jest niedostępne.')
                self.power(action)
        return True

    def _change_playback_setting(self, name, value):
        if value == getattr(self.player, name):
            return
        self.player.pause()
        getattr(self.player, 'set_' + name)(value)
        # pause drains old callbacks. EOF may have won the race after the
        # is_playing check; handle it before invalidating its generation.
        if self.player.position >= self.player.duration:
            self._handle_event(self.generation, 'ended', None)
            return
        self.generation += 1
        self.player.play()
        self.save()

    def change_volume(self, delta):
        if self.closed or not self.player.is_playing:
            return
        value = max(0, min(100, self.player.volume + delta))
        # Volume changes the running stream only; keep EOF generation intact.
        if value != self.player.volume:
            self.player.set_volume(value)
            self._save_playback_setting('volume')
        self.notify('Głośność {0} procent.'.format(value))

    def change_speed(self, delta):
        if self.closed or not self.player.is_playing:
            return
        value = max(1.0, min(2.0, self.player.speed + delta))
        if value == self.player.speed:
            return
        self._change_playback_setting('speed', value)
        self._save_playback_setting('speed')
        self.notify('Prędkość {0}×'.format(format(value, 'g').replace('.', ',')))

    def next_track(self, *, resume=None):
        self._change_track(1, resume=resume)

    def previous_track(self, *, resume=None):
        self._change_track(-1, resume=resume)

    def _change_track(self, delta, *, resume=None):
        if self.book_id is None:
            self.notify('Najpierw wybierz książkę.')
            return
        book = self.library.get_book(self.book_id)
        index = self.track_index + delta
        if not 0 <= index < len(book['tracks']):
            # A boundary is informational, not a playback/timer/gesture action.
            self.notify('Koniec książki.' if delta > 0 else 'Początek książki.')
            return
        if resume is None:
            session = self._adjustment
            resume = self.player.is_playing or bool(
                session and session.context == self.adjustment_context
                and not session.done and not session.cancelled
                and session.resume and session.allow_resume)
        if self._pending_transition is not None:
            self.generation += 1
        self._pending_transition = None
        self._start(self.book_id, index, autoplay=resume, save_selection=True)

    def remove(self, book_id):
        self.cancel_adjustment()
        if self._pending_transition is not None:
            self.generation += 1
        self._pending_transition = None
        if self.book_id == book_id:
            self._book_status_token.set()
            self._book_status_token = threading.Event()
            self._cancel_end_timer()
            self.generation += 1
            self.player.pause()
            self.save()
            self.book_id = None
        self.library.remove_book(book_id)

    def close(self):
        with self._power_lock:
            if self.closed:
                return
            self.closed = True
        self._book_status_token.set()
        self.cancel_adjustment()
        if self.sleep_timer is not None:
            self.sleep_timer.cancel()
        self._pending_transition = None
        self.generation += 1
        try:
            self.player.pause()
            self.save()
        finally:
            self.player.close()

    def book_status_snapshot(self):
        """Playback-worker snapshot; detached metadata goes to the probe worker."""
        snapshot = dict(token=self._book_status_token, metadata=None)
        if self.book_id is not None:
            snapshot.update(metadata=self.library.duration_snapshot(self.book_id),
                            probe=self.library.probe_durations)
        return snapshot

    @staticmethod
    def probe_book_status(snapshot, cancelled):
        """Separate worker: only changed media, never a library/player lock."""
        if snapshot['metadata'] is None:
            return []
        return snapshot['probe'](snapshot['metadata'],
                                 lambda: cancelled() or snapshot['token'].is_set())

    def complete_book_status(self, snapshot, results):
        """Playback worker: persist valid changes and render the latest position."""
        if self.closed or self.cancelled() or snapshot['token'].is_set():
            return None
        metadata = snapshot['metadata']
        if metadata is None:
            return 'Nie wybrano książki.'
        if metadata['book_id'] != self.book_id:
            return None
        durations = self.library.commit_durations(metadata, results)
        duration = self.player.duration
        if (self.track_index == len(metadata['tracks']) - 1
                and duration is not None and duration > 0
                and self.player.position >= duration):
            return 'Koniec książki.'
        if not durations or any(value is None for value in durations):
            return 'Czas do końca książki: nieznany.'
        total = sum(durations)
        position = max(0, min(durations[self.track_index], self.player.position))
        elapsed = sum(durations[:self.track_index]) + position
        return format_time(max(0, total - elapsed) / self.player.speed) + ' do końca książki.'

    def book_status(self):
        """Synchronous standalone API; the plugin splits these stages across workers."""
        snapshot = self.book_status_snapshot()
        durations = self.probe_book_status(snapshot, lambda: self.closed or self.cancelled())
        return self.complete_book_status(snapshot, durations) or (
            'Czas do końca książki: nieznany.')

    def status(self):
        if self.book_id is None:
            return 'Nie wybrano książki.'
        duration = max(0, self.player.duration)
        elapsed = max(0, min(duration, self.player.position))
        # Completion is an exact source position, never rounded spoken time.
        if duration > 0 and elapsed >= duration:
            book = self.library.get_book(self.book_id)
            if track_label(book, self.track_index).startswith('Wstęp'):
                return 'Koniec wstępu.'
            return 'Koniec odcinka.'
        remaining = max(0, duration - elapsed)
        return format_time(remaining / self.player.speed) + ' do końca odcinka.'

    def save(self):
        if self.book_id is not None:
            self.library.save_position(self.book_id, self.track_index, self.player.position)

    def add_bookmark(self):
        """Capture the loaded player's clock without pausing or saving progress."""
        if self.closed or self.cancelled():
            return None
        if self.book_id is None:
            self.notify('Najpierw uruchom książkę.')
            return None
        bookmark = self.library.add_bookmark(self.book_id, self.track_index, self.player.position)
        self.notify('Dodano zakładkę: {0}.'.format(bookmark['name']))
        return bookmark

    def start_bookmark(self, book_id, bookmark_id):
        if self.closed or self.cancelled():
            return
        bookmark = self.library.get_bookmark(book_id, bookmark_id)
        return self._start(book_id, bookmark['track_index'], bookmark['position'],
                           capture_bookmark_return=True)

    def return_from_bookmark(self, book_id):
        """Resume the first excursion point; consume it only after a safe return."""
        if self.closed or self.cancelled():
            return False
        point = self.library.get_bookmark_return(book_id)
        if point is None:
            return False
        self._start(book_id, point['track_index'], point['position'])
        if self.closed or self.cancelled():
            return False  # Shutdown during loading must leave a retryable anchor.
        self.library.complete_bookmark_return(book_id)
        self.notify('Powrót do słuchania.')
        return True

    def start(self, book_id, index=None):
        return self._start(book_id, index)

    def _start(self, book_id, index=None, bookmark_position=None, *, capture_bookmark_return=False,
               autoplay=True, save_selection=False):
        self.cancel_adjustment()
        self._pending_transition = None
        if self.closed:
            return
        # Stop old audio before probing a new track. Its callback must retain
        # the OLD controller generation, even when probing takes seconds.
        self.player.pause()
        if capture_bookmark_return:
            # Capture the settled clock before _start can save/jump. An anchor
            # for a different book always comes from THAT book's progress.
            if self.closed or self.cancelled():
                return
            if book_id == self.book_id:
                return_track, return_position = self.track_index, self.player.position
            else:
                target = self.library.get_book(book_id)
                return_track, return_position = target['track_index'], target['position']
            self.library.save_bookmark_return(book_id, return_track, return_position)
        # A manual resume may be FIFO-ahead of the queued EOF handler. Claim
        # the actual end AFTER pause's callback barrier, before invalidation.
        if (index is None and book_id == self.book_id
                and self._claim_natural_end(self.generation)):
            if self.check_timer(ended=True):
                return  # An end/count timer outranks this resume command.
        self.save()
        book = self.library.get_book(book_id)
        track = book['track_index'] if index is None else index
        position = book['position'] if index is None else 0
        if bookmark_position is not None:
            position = bookmark_position
        if (book_id, track) != (self.book_id, self.track_index):
            self._cancel_end_timer()
        self.generation += 1
        duration = self.library.track_duration(book_id, track)
        if index is None and position >= duration and track + 1 < len(book['tracks']):
            # Resolve completion only on manual resume, never pre-load at EOF.
            track += 1
            position = 0
            duration = self.library.track_duration(book_id, track)
        path = self.library.track_path(book_id, track)
        self.player.load(path, position=position, duration=duration)
        if self.book_id != book_id:
            self._book_status_token.set()
            self._book_status_token = threading.Event()
        self.book_id = book_id
        self.track_index = track
        # Loading/probing/saving can block while NVDA terminates the plugin.
        # Keep the loaded identity consistent for cleanup, but never start audio.
        if self.closed or self.cancelled():
            return
        if bookmark_position is not None and position >= duration:
            # play() rewinds an ended track; a bookmark must never do that.
            self.notify('Koniec {0}.'.format(track_label(book, track)
                .replace('Odcinek', 'odcinka').replace('Wstęp', 'wstępu')))
            return
        if index is None and position >= duration:
            self.notify('Koniec książki.')
            return
        if save_selection:
            self.save()
            if self.closed or self.cancelled():
                return
        if autoplay:
            self.player.play()
        self.notify(track_label(book, track) + '.')

    def toggle(self, selected_book_id=None, force_pause=False):
        adjusting_playback = force_pause or bool(self._adjustment and self._adjustment.resume
                                                 and not self._adjustment.cancelled)
        self.cancel_adjustment()
        if adjusting_playback and not self.player.is_playing:
            self.player.pause()
            self.generation += 1
            self.save()
            self.notify('Pauza.')
            return
        if self.book_id is None:
            if selected_book_id is not None:
                self.start(selected_book_id)
                return
            self.notify('Najpierw wybierz książkę w bibliotece.')
            return
        if self._pending_transition is not None:
            self._pending_transition = None
            self.player.pause()
            self.generation += 1
            self.save()
            self.notify('Pauza.')
        elif self.player.is_playing:
            self.player.pause()
            self.save()
            self.notify('Pauza.')
        elif self.player.position >= self.player.duration:
            self.start(self.book_id)
        else:
            self.player.play()
            self.notify('Wznowiono odtwarzanie.')
