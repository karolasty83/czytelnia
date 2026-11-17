"""Audio output settings and child-only launch environment, without audio hardware."""
import importlib.util
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'output_test_player', ROOT / 'addon/globalPlugins/czytelnia/player.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class Pipe:
    def __init__(self):
        self.chunks = queue.Queue()

    def read1(self, size):
        return self.chunks.get(timeout=5)

    def close(self):
        pass


class Process:
    """Transport substitute; Player's real watcher and clocks still run."""
    def __init__(self, args, env):
        self.args, self.env = args, env
        self.stderr = Pipe()
        self.returncode = None
        self.done = threading.Event()
        self.closed = False
        self.clock(float(args[args.index('-ss') + 1]))

    def clock(self, seconds, anchor=True):
        if anchor:
            self.stderr.chunks.put((
                f'[ashowinfo@native_anchor @ 123] n:0 pts:{round(seconds * 48000)} '
                'fmt:fltp rate:48000\n').encode())
        self.stderr.chunks.put(f'{seconds:.5f} M-A: 0.0\r'.encode())

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1
        self.done.set()
        self.stderr.chunks.put(b'')

    kill = terminate

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('fake', timeout)
        return self.returncode

    def close(self):
        self.closed = True


class OutputLaunchTests(unittest.TestCase):
    def setUp(self):
        self.processes, self.instances, self.events = [], [], []
        self.control = mock.patch.object(p.VolumeControl, 'set_volume', return_value='0 OK')
        self.volume_calls = self.control.start()
        self.addCleanup(self.control.stop)
        popen = mock.patch.object(p.subprocess, 'Popen', side_effect=self.spawn)
        popen.start()
        self.addCleanup(popen.stop)
        self.addCleanup(self.close_players)

    def close_players(self):
        for instance in self.instances:
            instance.close()

    def spawn(self, args, **kwargs):
        process = Process(args, kwargs.get('env'))
        self.processes.append(process)
        return process

    def make(self, native):
        instance = p.Player('ffplay.exe', 'ffprobe.exe',
                            lambda *event: self.events.append(event), volume=37,
                            use_native=native, native_factory=self.spawn)
        self.instances.append(instance)
        instance.load(__file__, position=12.5, duration=100)
        return instance

    def test_same_output_is_noop_even_while_playing(self):
        for native in (False, True):
            with self.subTest(native=native):
                instance = self.make(native)
                instance.set_output_device('Speakers')
                instance.set_speed(1.5)
                instance.play()
                process, generation = self.processes[-1], instance._generation
                launches, acknowledgements = len(self.processes), self.volume_calls.call_count
                instance.set_output_device('Speakers')
                self.assertIs(instance._process, process)
                self.assertIsNone(process.poll())
                self.assertEqual(instance._generation, generation)
                self.assertEqual(len(self.processes), launches)
                self.assertEqual(self.volume_calls.call_count, acknowledgements)
                self.assertEqual((instance.position, instance.volume, instance.speed), (12.5, 37, 1.5))
                instance.close()

    def test_output_change_preserves_paused_position_volume_and_speed(self):
        for native in (False, True):
            with self.subTest(native=native):
                instance = self.make(native)
                instance.set_speed(2)
                instance.play()
                instance.pause()
                launches = len(self.processes)
                instance.set_output_device('Headphones')
                self.assertEqual(len(self.processes), launches)
                self.assertFalse(instance.is_playing)
                self.assertEqual((instance.position, instance.volume, instance.speed), (12.5, 37, 2))
                instance.play()
                self.assertEqual(self.processes[-1].env['SDL_AUDIO_DEVICE_NAME'], 'Headphones')
                instance.close()

    def test_output_change_resumes_at_last_clock_with_same_volume_and_speed(self):
        for native in (False, True):
            with self.subTest(native=native):
                instance = self.make(native)
                instance.set_speed(2)
                instance.play()
                old = self.processes[-1]
                old.clock(16, anchor=False)
                deadline = time.monotonic() + 2
                while instance.position != 19.5 and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertEqual(instance.position, 19.5, 'real watcher must publish the source clock')
                launches = len(self.processes)
                instance.set_output_device('Headphones')
                new = self.processes[-1]
                self.assertEqual(len(self.processes), launches + 1)
                self.assertIsNot(new, old)
                self.assertIsNotNone(old.poll())
                self.assertTrue(instance.is_playing)
                self.assertEqual(new.args[new.args.index('-ss') + 1], '19.5')
                self.assertIn('atempo=2', new.args[new.args.index('-af') + 1])
                self.assertEqual(new.env['SDL_AUDIO_DEVICE_NAME'], 'Headphones')
                self.assertEqual(self.volume_calls.call_args.args[0], 37)
                self.assertEqual((instance.position, instance.volume, instance.speed), (19.5, 37, 2))
                instance.close()
        self.assertEqual(self.events, [], 'replaced children must not emit stale events')

    def test_loading_another_part_preserves_output_for_next_launch(self):
        for native in (False, True):
            with self.subTest(native=native), mock.patch.dict(
                    os.environ, {'SDL_AUDIO_DEVICE_NAME': 'Inherited'}, clear=True):
                instance = self.make(native)
                instance.set_output_device('Headphones')
                instance.set_speed(1.5)
                instance.play()
                instance.load(__file__, position=7, duration=50)
                self.assertFalse(instance.is_playing)
                self.assertEqual(instance.output_device, 'Headphones')
                instance.play()
                self.assertEqual(self.processes[-1].env['SDL_AUDIO_DEVICE_NAME'], 'Headphones')
                self.assertEqual((instance.position, instance.volume, instance.speed), (7, 37, 1.5))
                instance.set_output_device('')
                self.assertNotIn('SDL_AUDIO_DEVICE_NAME', self.processes[-1].env)
                self.assertEqual(os.environ['SDL_AUDIO_DEVICE_NAME'], 'Inherited')
                instance.close()

    def test_native_uses_wasapi_but_preserves_explicit_dummy_driver(self):
        for native in (False, True):
            for driver in (None, 'directsound', 'dummy'):
                with self.subTest(native=native, driver=driver), mock.patch.dict(
                        os.environ, {}, clear=True):
                    if driver is not None:
                        os.environ['SDL_AUDIODRIVER'] = driver
                    parent = dict(os.environ)
                    instance = self.make(native)
                    instance.set_output_device('CoreAudio friendly name')
                    instance.play()
                    expected = 'wasapi' if native and driver != 'dummy' else driver
                    self.assertEqual(self.processes[-1].env.get('SDL_AUDIODRIVER'), expected)
                    self.assertEqual(dict(os.environ), parent)
                    instance.close()

    def test_explicit_name_passes_intact_only_to_each_child(self):
        name = '  Głośniki (USB) — 音声 & "one"  '
        for native in (False, True):
            with self.subTest(native=native), mock.patch.dict(
                    os.environ, {'SDL_AUDIO_DEVICE_NAME': 'Inherited',
                                 'SDL_AUDIODRIVER': 'dummy'}, clear=True):
                parent = dict(os.environ)
                instance = self.make(native)
                instance.set_output_device(name)
                instance.play()
                process = self.processes[-1]
                actual = process.env.get('SDL_AUDIO_DEVICE_NAME')
                if os.name == 'nt':
                    self.assertEqual(actual.encode('mbcs'), name.encode('utf-8'))
                else:
                    self.assertEqual(actual, name)
                self.assertNotIn(name, process.args)
                self.assertEqual(dict(os.environ), parent)
                instance.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows ANSI/UTF-8 environment boundary')
    def test_windows_sdl_receives_utf8_bytes_for_nonascii_device_names(self):
        name = 'Speakers/Headphones (2 — Realtek(R) Audio)'
        instance = self.make(True)
        instance.set_output_device(name)
        instance.play()
        child_value = self.processes[-1].env['SDL_AUDIO_DEVICE_NAME']
        self.assertEqual(child_value.encode('mbcs'), name.encode('utf-8'))
        self.assertEqual(instance.output_device, name, 'stored/displayed name stays Unicode')

    def test_default_clears_inherited_device_only_in_each_child(self):
        for native in (False, True):
            with self.subTest(native=native), mock.patch.dict(
                    os.environ, {'SDL_AUDIO_DEVICE_NAME': 'Inherited output',
                                 'SDL_AUDIODRIVER': 'dummy', 'OUTPUT_TEST': 'retained'}):
                parent = dict(os.environ)
                instance = self.make(native)
                instance.play()
                child = self.processes[-1].env
                self.assertIsNotNone(child, 'each transport needs a child environment')
                self.assertNotIn('SDL_AUDIO_DEVICE_NAME', child)
                self.assertEqual(child['OUTPUT_TEST'], 'retained')
                self.assertEqual(child['SDL_AUDIODRIVER'], 'dummy')
                self.assertEqual(dict(os.environ), parent)
                instance.close()


class OutputSettingTests(unittest.TestCase):
    def test_unrepresentable_environment_is_rejected_before_changing_player(self):
        instance = p.Player('ffplay.exe', 'ffprobe.exe')
        self.addCleanup(instance.close)
        with mock.patch.object(p, 'sdl_output_name', side_effect=ValueError('code page')), \
             mock.patch.object(instance, '_halt') as halt:
            with self.assertRaises(ValueError):
                instance.set_output_device('Incompatible device')
        self.assertEqual(instance.output_device, '')
        halt.assert_not_called()

    def test_output_is_default_readonly_validated_and_survives_load(self):
        instance = p.Player('ffplay.exe', 'ffprobe.exe')
        self.addCleanup(instance.close)
        self.assertEqual(instance.output_device, '')
        with self.assertRaises(AttributeError):
            instance.output_device = 'speaker'
        for name in (' Głośniki (USB) — 音声 ', 'x' * 1024, ''):
            instance.set_output_device(name)
            self.assertEqual(instance.output_device, name)
        instance.set_output_device('Głośniki')
        for invalid in (None, True, 1, b'speaker', 'a\0b', 'x' * 1025):
            with self.subTest(value=repr(invalid)[:40]):
                with self.assertRaises(ValueError):
                    instance.set_output_device(invalid)
                self.assertEqual(instance.output_device, 'Głośniki')
        instance.load(__file__, position=12.5, duration=100)
        self.assertEqual(instance.output_device, 'Głośniki')
        self.assertFalse(instance.is_playing)
        instance.close()
        with self.assertRaises(RuntimeError):
            instance.set_output_device('')


if __name__ == '__main__':
    unittest.main()
