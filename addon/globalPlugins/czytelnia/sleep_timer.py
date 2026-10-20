"""Thread-safe transient sleep timer; the caller performs consumed actions."""
import threading
import time


def _valid_target(target):
    return (type(target) is tuple and len(target) == 2
            and type(target[0]) is str and bool(target[0])
            and type(target[1]) is int and target[1] >= 0)


class SleepTimer:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._state = None
        self._deadline = None
        self._lock = threading.RLock()

    def arm(self, minutes=None, action='stop', target=None, tracks=None):
        """Replace atomically; invalid settings leave the previous timer intact.

        End/count modes bind to (book ID, starting index). Count includes the
        current part and only consumes distinct, sequential natural EOFs.
        """
        if tracks is not None:
            if type(tracks) is not int or tracks <= 0:
                raise ValueError('Liczba odcinków musi być dodatnią liczbą całkowitą.')
            if minutes is not None:
                raise ValueError('Wybierz minuty albo liczbę odcinków, nie oba tryby.')
        if type(action) is not str or action not in ('stop', 'sleep', 'shutdown'):
            raise ValueError('Unknown sleep timer action')
        if minutes is not None and (type(minutes) is not int or not 1 <= minutes <= 1440):
            raise ValueError('Minutes must be an integer from 1 through 1440')
        if (minutes is None or target is not None) and not _valid_target(target):
            raise ValueError('Target must be a (book ID, track index) tuple')
        mode = 'tracks' if tracks is not None else ('end' if minutes is None else 'minutes')
        state = dict(mode=mode, action=action, remaining=None,
                     target=target, minutes=minutes)
        if tracks is not None:
            state.update(tracks=tracks, remaining_tracks=tracks)
        with self._lock:
            self._deadline = None if minutes is None else self._clock() + minutes * 60
            self._state = state

    def cancel(self):
        with self._lock:
            active = self._state is not None
            self._state = None
            self._deadline = None
            return active

    def poll(self, target=None, ended=False):
        with self._lock:
            if self._state is None:
                return None
            if self._state['mode'] == 'tracks':
                book, first = self._state['target']
                expected = (book, first + self._state['tracks'] - self._state['remaining_tracks'])
                if not ended or not _valid_target(target) or target != expected:
                    return None
                self._state['remaining_tracks'] -= 1
                due = self._state['remaining_tracks'] == 0
            else:
                due = (self._clock() >= self._deadline if self._deadline is not None
                       else ended and _valid_target(target) and target == self._state['target'])
            if due:
                action = self._state['action']
                self.cancel()
                return action
            return None

    def snapshot(self):
        with self._lock:
            if self._state is None:
                return None
            result = self._state.copy()
            if self._deadline is not None:
                result['remaining'] = max(0.0, self._deadline - self._clock())
            return result
