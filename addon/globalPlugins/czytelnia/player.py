"""Small, NVDA-independent ffplay backend; ffplay's audio clock is authoritative."""
import math
import os
import tempfile
import re
import json
from pathlib import Path
import subprocess
import threading
import traceback

if __package__:
    from .zmq_control import VolumeControl, ControlError
    from .native_seek import NativeProcess, NativeClock
else:
    # Standalone probes/tests must not import the NVDA plugin package.
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "czytelnia_zmq_control", Path(__file__).with_name("zmq_control.py"))
    _control_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_control_module)
    VolumeControl, ControlError = _control_module.VolumeControl, _control_module.ControlError
    _spec = importlib.util.spec_from_file_location(
        "czytelnia_native_seek", Path(__file__).with_name("native_seek.py"))
    _native_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_native_module)
    NativeProcess, NativeClock = _native_module.NativeProcess, _native_module.NativeClock

PROBE_TIMEOUT = 10.0
PROCESS_TIMEOUT = 2.0
SEEK_TIMEOUT = 3.0

_AUDIO_CLOCK = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s+M-A:\s")


def parse_clock(line):
    """Return ffplay's raw audio clock, never the A/V difference or NaN.

    With atempo this retains the initial seek offset but counts output seconds;
    the process-specific source mapping is applied by Player._watch.
    """
    match = _AUDIO_CLOCK.match(line)
    if match:
        value = float(match.group(1))
        if math.isfinite(value):
            return max(0.0, value)
    return None


def _validate_duration(value):
    try:
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return float(value)
    except OverflowError:
        pass
    raise ValueError('Media has no finite positive duration')


def probe_duration(path, ffprobe_path):
    """Bounded local-only ffprobe; also used when importing library media."""
    path = Path(path).resolve()
    result = subprocess.run(
        [str(ffprobe_path), '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'json', '-protocol_whitelist', 'file,pipe', str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace', timeout=PROBE_TIMEOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), shell=False, check=True)
    value = json.loads(result.stdout)['format']['duration']
    return _validate_duration(float(value) if isinstance(value, str) else value)


class _NativePlayback:
    def __init__(self, speed):
        self.clock = NativeClock(speed)
        self.ready = threading.Event()
        # Unlike ready, this also wakes play() on decoder failure or drained exit.
        self.startup = threading.Event()
        self.changed = threading.Event()
        self.seeking = False
        self.has_seek = False
        self.error = None
        self.origin = 0.0
        self.drained = threading.Event()
        self.outcome = None
        self.failure = None


class Player:
    """Local-file player. Positions and seeks are absolute seconds.

    load raises on invalid media/probe failure. Playback events are delivered on
    worker threads, not the NVDA GUI thread; callers must marshal GUI work.
    volume is an integer 0..100 (default 100); speed is 0.5..2 in half steps
    (default 1). Both are read-only properties changed with set_volume/set_speed.
    Volume changes use the running process's azmq filter, without pausing or
    resetting its clock. Speed changes relaunch at the last source position.
    Paused playback stays paused. load preserves both settings across parts.
    diagnostic(stage, detail), if supplied, receives error-only technical context
    separately from on_event's spoken detail. Callback exceptions are ignored.
    """

    def __init__(self, ffplay_path, ffprobe_path, on_event=None, volume=100, *,
                 use_native=None, native_factory=None, diagnostic=None):
        self._ffplay = str(ffplay_path)
        self._ffprobe = str(ffprobe_path)
        self._on_event = on_event
        self._diagnostic_callback = diagnostic
        self._volume = self._validate_volume(volume)
        self._speed = 1.0
        self._lock = threading.RLock()
        self._operations = threading.RLock()
        self._path = None
        self._position = 0.0
        self._duration = 0.0
        self._process = None
        self._volume_control = None
        self._diagnostic = b""
        self._generation = 0
        self._workers = set()
        self._closed = False
        self._use_native = ((os.name == "nt" or native_factory is not None)
                            if use_native is None else bool(use_native))
        self._native_factory = native_factory or NativeProcess
        self._gain_directory = None
        self._native_state = None
        self._notified_generation = None

    @property
    def supports_live_seek(self):
        return self._use_native

    def _write_gain(self, control, value):
        # Only our unique directory is used as cwd; all media/exe paths are absolute.
        if self._gain_directory is None:
            self._gain_directory = tempfile.TemporaryDirectory(prefix="czytelnia-gain-")
        directory = Path(self._gain_directory.name)
        temporary = directory / "gain.tmp"
        try:
            temporary.write_text(f"0.0 {control.target} volume {value / 100:g};\n", encoding="ascii")
            os.replace(temporary, directory / "gain.cmd")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_volume(value):
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError("Volume must be an integer from 0 to 100")
        return value

    @property
    def volume(self):
        with self._lock:
            return self._volume

    def set_volume(self, value):
        value = self._validate_volume(value)
        with self._operations:
            self._ensure_open()
            with self._lock:
                if self._volume == value:
                    return
                process, control = self._process, self._volume_control
                native, generation = self._native_state, self._generation
            if process is not None and process.poll() is None:
                if control is None:
                    raise ControlError('Running player has no volume control')
                if self._use_native:
                    # The next graph must see the requested gain from its FIRST
                    # frame. An atomic-file failure leaves live gain/settings alone.
                    self._write_gain(control, value)
                try:
                    control.set_volume(value, alive=lambda: process.poll() is None)
                except ControlError as error:
                    if self._use_native:
                        # An exited child may still have unread clock/error lines.
                        # Wait for evidence, not the worker blocked on this barrier.
                        if (process.poll() is not None and native.drained.wait(PROCESS_TIMEOUT)
                                and native.outcome[0] == 'ended'):
                            # Preserve proven EOF before invalidating its generation.
                            # The callback may start another part; never commit this
                            # failed volume request to that new process/settings.
                            self._notify(generation, *native.outcome)
                            return
                    self._report_diagnostic('playback.volume', error=error, process=process,
                                            requested_volume=value)
                    if self._use_native:
                        # ACK failure is uncertain (the command may have applied).
                        # Stop; a future launch rewrites the old committed setting.
                        self._halt()
                    raise
            with self._lock:
                self._volume = value


    @property
    def speed(self):
        with self._lock:
            return self._speed

    def set_speed(self, value):
        if (type(value) not in (int, float)
                or value not in (0.5, 1.0, 1.5, 2.0)):
            raise ValueError("Speed must be from 0.5 to 2.0 in steps of 0.5")
        self._set_playback_setting('_speed', float(value))

    def _set_playback_setting(self, name, value):
        with self._operations:
            self._ensure_open()
            with self._lock:
                if getattr(self, name) == value:
                    return
                was_playing = self._process is not None
            self._halt()
            with self._lock:
                setattr(self, name, value)
            if was_playing and self.position < self.duration:
                self.play()

    @property
    def position(self):
        with self._lock:
            return self._position

    @property
    def duration(self):
        with self._lock:
            return self._duration

    @property
    def is_playing(self):
        with self._lock:
            return self._process is not None

    def load(self, path, position=0, *, duration=None):
        """Use a validated library duration; standalone loads may still ffprobe."""
        with self._operations:
            self._ensure_open()
            path = Path(path).resolve()
            if not path.is_file():
                raise FileNotFoundError(str(path))
            position = float(position)
            if not math.isfinite(position):
                raise ValueError("Position must be finite")
            duration = (probe_duration(path, self._ffprobe) if duration is None
                        else _validate_duration(duration))
            self._halt()
            with self._lock:
                self._path = str(path)
                self._duration = duration
                self._position = min(duration, max(0.0, float(position)))

    def play(self):
        with self._operations:
            self._ensure_open()
            with self._lock:
                if self._process is not None:
                    return
                if self._path is None:
                    raise RuntimeError("No media loaded")
                if self._position >= self._duration:
                    self._position = 0.0
                self._generation += 1
                generation = self._generation
                self._diagnostic = b""
                start_position, speed = self._position, self._speed
                # Never exceed atempo=2: chaining avoids its sample-skipping mode.
                tempo = ('atempo=2,atempo={:g}'.format(speed / 2) if speed > 2
                         else 'atempo={:g}'.format(speed))
                control = None
                try:
                    control = VolumeControl()
                    graph = tempo + "," + control.filter(self._volume)
                    args = [self._ffplay, "-hide_banner", "-vn", "-autoexit",
                            "-stats", "-volume", "100", "-ss", str(start_position)]
                    if self._use_native:
                        self._write_gain(control, self._volume)
                        args[0] = str(Path(self._ffplay).resolve())
                        graph = ("ashowinfo@native_anchor," + tempo +
                                 ",asendcmd=filename=gain.cmd," + control.filter(0))
                    else:
                        args.append("-nodisp")
                    args.extend(["-af", graph, "-protocol_whitelist", "file,pipe", self._path])
                    if self._use_native:
                        # 30000px private windows exceed common GPU texture limits.
                        # Only this child gets SDL video defaults; retain real audio.
                        env = dict(os.environ, SDL_VIDEODRIVER="windows",
                                   SDL_RENDER_DRIVER="software")
                        process = self._native_factory(args, cwd=self._gain_directory.name, env=env)
                    else:
                        process = subprocess.Popen(
                            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except OSError as error:
                    if control is not None:
                        control.close()
                    self._report_diagnostic('playback.start', error=error)
                    self._start_worker(self._notify, generation, "error", str(error), True)
                    return
                self._process = process
                self._volume_control = control
                drained, outcome = threading.Event(), []
                native = _NativePlayback(speed) if self._use_native else None
                self._native_state = native
                self._start_worker(self._watch, process, generation, start_position, speed,
                                   drained, outcome, native)
            # Drain stderr concurrently; never hold the clock lock during I/O.
            # Validate the exact random filter target before reporting startup.
            stage = 'playback.start.readiness' if native is not None else 'playback.start.volume'
            try:
                if native is not None:
                    # ffplay first binds azmq in a disposable graph, then rebuilds
                    # it for the first decoded frame. Anchor + finite clock prove
                    # the playback graph exists; TCP connect alone does not.
                    native.startup.wait(PROCESS_TIMEOUT)
                    with self._lock:
                        if native.drained.is_set():
                            if native.outcome[0] == 'ended':
                                return  # Leave this generation's EOF to the watcher.
                            raise ControlError(native.outcome[1]) from native.failure
                        if native.error:
                            raise ControlError(native.error)
                        if not native.ready.is_set():
                            raise ControlError('No initial native audio clock')
                stage = 'playback.start.volume'
                control.set_volume(self._volume, alive=lambda: process.poll() is None)
            except ControlError as error:
                # A short remaining tail can finish before azmq acknowledges.
                # Wait only for drained stderr + actual exit evidence, never for
                # the worker: its callback needs the _operations lock we hold.
                if (stage == 'playback.start.volume'
                        and drained.wait(PROCESS_TIMEOUT) and outcome[0] == 'ended'):
                    return  # The watcher alone dispatches this generation's EOF.
                with self._lock:
                    detail = str(error)
                    if self._diagnostic and stage == 'playback.start.volume':
                        detail += ': ' + self._diagnostic.decode('utf-8', 'replace').strip()
                self._report_diagnostic(stage, error=error, process=process)
                self._halt()
                with self._lock:
                    self._start_worker(self._notify, self._generation, 'error', detail, True)

    def _start_worker(self, target, *args):
        def run():
            try:
                target(*args)
            finally:
                with self._lock:
                    self._cleanup_gain_directory()
                    self._workers.discard(threading.current_thread())
        worker = threading.Thread(target=run, name="Czytelnia", daemon=True)
        self._workers.add(worker)
        worker.start()

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Player is closed")

    def _halt(self):
        # Invalidate BEFORE signalling: even a concurrent clean exit is stale.
        with self._lock:
            self._generation += 1
            process = self._process
            self._process = None
            control, self._volume_control = self._volume_control, None
        if control is not None:
            control.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=PROCESS_TIMEOUT)

    def pause(self):
        with self._operations:
            self._halt()

    def seek(self, seconds, *, before_seek=None):
        seconds = float(seconds)
        if not math.isfinite(seconds):
            raise ValueError("Position must be finite")
        with self._operations:
            self._ensure_open()
            with self._lock:
                target = min(self._duration, max(0.0, seconds))
                process, native = self._process, self._native_state
            if self._use_native and process is not None:
                if process.poll() is None and target < self.duration:
                    if not native.ready.wait(SEEK_TIMEOUT):
                        self._halt()
                        raise RuntimeError("No initial native audio clock")
                # A watcher may have drained EOF but be blocked on our callback
                # barrier. Deliver that OLD event before the controller moves
                # its generation; never wait for/join the callback thread here.
                if process.poll() is not None:
                    if not native.drained.wait(PROCESS_TIMEOUT):
                        self._halt()
                        raise RuntimeError("Native exit drain timeout")
                    self._notify(self._generation, *native.outcome)
                    with self._lock:
                        process, native = self._process, self._native_state
            was_playing = process is not None
            if self._use_native and process is not None and target < self.duration:
                if before_seek is not None:
                    before_seek()
                try:
                    with self._lock:
                        if target == self._position:
                            return
                        native.clock.begin_seek()
                        native.changed.clear()
                        native.seeking = native.has_seek = True
                        native.error = None
                        self._volume_control.reset_connection()
                        process.seek(target, self._duration)
                    if not native.changed.wait(SEEK_TIMEOUT) or native.error:
                        raise RuntimeError(native.error or "Native seek confirmation timeout")
                except Exception:
                    self._halt()
                    raise
                return
            self._halt()
            if before_seek is not None:
                before_seek()
            with self._lock:
                self._position = target
            if was_playing and self.position < self.duration:
                self.play()

    def stop(self):
        with self._operations:
            self._halt()
            with self._lock:
                self._position = 0.0

    def close(self):
        with self._operations:
            self._closed = True
            self._halt()
            with self._lock:
                workers = tuple(self._workers)
        # Never join while holding the command lock: workers dispatch through it.
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=PROCESS_TIMEOUT)
        self._cleanup_gain_directory()

    def _cleanup_gain_directory(self):
        with self._lock:
            # A close timeout must not unlink a live child's initializer. The
            # final reader owns eventual cleanup, including reentrant close.
            if (self._closed and self._gain_directory is not None
                    and not self._workers - {threading.current_thread()}):
                self._gain_directory.cleanup()
                self._gain_directory = None

    def _report_diagnostic(self, stage, detail=None, *, error=None, process=None,
                           requested_volume=None):
        # Diagnostics must never change playback/notification policy. No locals,
        # environment, command lines or media bytes are collected here.
        if self._diagnostic_callback is None:
            return
        try:
            with self._lock:
                process = self._process if process is None else process
                context = (f"pid={getattr(process, 'pid', None)} "
                           f"returncode={getattr(process, 'returncode', None)} "
                           f"position={self._position} duration={self._duration} "
                           f"speed={self._speed} volume={self._volume}")
                stderr = self._diagnostic[-8192:].decode('utf-8', 'replace')
            if requested_volume is not None:
                context += f" requested_volume={requested_volume}"
            parts = [context]
            if error is not None:
                parts.append(f"control_stage={getattr(error, 'diagnostic_stage', None)}")
                current, seen = error, set()
                while current is not None and id(current) not in seen:
                    seen.add(id(current))
                    parts.append(f"{type(current).__name__}: "
                                 f"errno={getattr(current, 'errno', None)} "
                                 f"winerror={getattr(current, 'winerror', None)}")
                    current = current.__cause__ or current.__context__
                parts.append(''.join(traceback.format_exception(
                    type(error), error, error.__traceback__))[-16384:])
            if detail:
                parts.append(str(detail)[-8192:])
            if stderr:
                parts.append('stderr (last 8192 bytes):\n' + stderr)
            self._diagnostic_callback(stage, '\n'.join(parts))
        except Exception:
            pass

    def _notify(self, generation, event, detail, diagnostic_logged=False, error=None):
        # Serialise generation check + callback against load/pause/seek/close.
        # The lock is reentrant so an ended callback may load the next chapter.
        with self._operations:
            with self._lock:
                if generation != self._generation or generation == self._notified_generation:
                    return
                self._notified_generation = generation
                if event == "error" and not diagnostic_logged:
                    self._report_diagnostic('playback.error', detail, error=error)
                self._process = None
                control, self._volume_control = self._volume_control, None
                if control is not None:
                    control.close()
                if event == "ended":
                    self._position = self._duration
            if self._on_event:
                self._on_event(event, detail)

    def _watch(self, process, generation, start_position, speed, drained, outcome, native=None):
        pending = b""
        diagnostic = b""
        saw_clock = False
        event, detail = "error", "ffplay produced no audio clock"
        failure = None

        def consume(line):
            nonlocal saw_clock
            text = line.decode("utf-8", "replace")
            if native is not None:
                with self._lock:
                    if generation != self._generation:
                        return
                    if re.search(r"error while seeking|Error (?:initializing|reinitializing|opening|applying|parsing|processing filtergraph)"
                                 r"|Failed to (?:configure|open)|Invalid data|Could not (?:bind|read)|Cannot read file", text):
                        native.error = text.strip()
                        native.changed.set()
                        native.startup.set()
                    if native.error:
                        return
                    origin = re.search(r"Duration:.*?start:\s*(-?\d+(?:\.\d+)?)", text)
                    if origin is not None:
                        value = float(origin[1])
                        if math.isfinite(value):
                            # ffplay source PTS retains the container's timestamp
                            # origin (e.g. MP3 encoder delay); our positions do not.
                            native.origin = value
                    source = native.clock.feed(text)
                    if source is not None:
                        saw_clock = True
                        self._position = min(self._duration, max(
                            0.0 if native.has_seek else start_position, source - native.origin))
                        native.ready.set()
                        native.startup.set()
                        if native.seeking:
                            native.seeking = False
                            native.changed.set()
                return
            clock = parse_clock(text)
            if clock is not None:
                saw_clock = True
                with self._lock:
                    if generation == self._generation:
                        # ffplay 8 + atempo retains the initial seek PTS, then
                        # reports OUTPUT elapsed seconds. Measured by the silent
                        # tools/probe_player_speed.py; never derive position from
                        # wall time or multiply the absolute seek offset.
                        source = start_position + (clock - start_position) * speed
                        # Initial audio-buffer latency may report before -ss.
                        self._position = min(self._duration, max(self._position, source))

        try:
            while True:
                # read1 returns available bytes: read(4096) can hide many seconds
                # of status, and readline alone waits for LF whereas stats use CR.
                chunk = process.stderr.read1(4096)
                if not chunk:
                    break
                diagnostic = (diagnostic + chunk)[-8192:]
                with self._lock:
                    if generation == self._generation:
                        self._diagnostic = diagnostic
                lines = re.split(b"[\r\n]", pending + chunk)
                pending = lines.pop()[-8192:]
                for line in lines:
                    consume(line)
            consume(pending)
            code = process.wait(timeout=PROCESS_TIMEOUT)
            if native is not None and (native.error or native.seeking):
                detail = native.error or "ffplay exited before native seek confirmation"
            elif code == 0 and saw_clock:
                event, detail = "ended", None
            else:
                detail = "ffplay exited with code {}: {}".format(
                    code, diagnostic.decode("utf-8", "replace").strip() or detail)
        except (OSError, subprocess.TimeoutExpired) as error:
            failure = error
            detail = str(error)
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=PROCESS_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                detail += "; cleanup: " + str(cleanup_error)
        finally:
            process.stderr.close()
            if self._use_native:
                process.close()
        # Publish before _notify tries to acquire the startup command lock.
        # A zero process return code without a finite audio clock is not EOF.
        outcome.append(event)
        drained.set()
        if native is not None:
            with self._lock:
                native.outcome = (event, detail)
                native.failure = failure
                native.drained.set()
                native.startup.set()
                if native.seeking:
                    native.error = detail or "ffplay exited before native seek confirmation"
                    native.changed.set()
        self._notify(generation, event, detail, False, failure)
