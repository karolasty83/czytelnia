"""Core Audio enumeration tests, executable without NVDA or packages."""
import ctypes
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'standalone_audio_outputs', ROOT / 'addon/globalPlugins/czytelnia/audio_outputs.py')
audio = importlib.util.module_from_spec(SPEC)
if Path(SPEC.origin).exists():
    SPEC.loader.exec_module(audio)


class AudioOutputsTests(unittest.TestCase):
    def test_non_windows_fails_explicitly(self):
        self.assertTrue(hasattr(audio, 'list_output_devices'), 'enumeration API missing')
        with mock.patch.object(audio.sys, 'platform', 'linux'):
            with self.assertRaisesRegex(OSError, 'Windows'):
                audio.list_output_devices()

    @unittest.skipUnless(__import__('sys').platform == 'win32', 'native Windows smoke')
    def test_native_windows_enumeration_and_existing_sta(self):
        import threading
        results = []
        failures = []

        def enumerate_in_sta():
            ole32 = ctypes.WinDLL('ole32.dll')
            ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            ole32.CoInitializeEx.restype = ctypes.c_int32
            ole32.CoUninitialize.argtypes = []
            ole32.CoUninitialize.restype = None
            initialized = ole32.CoInitializeEx(None, 2)
            try:
                self.assertEqual(initialized, 0)
                results.append(audio.list_output_devices())
                # Our MTA attempt must not uninitialize this thread's STA.
                second = ole32.CoInitializeEx(None, 2)
                try:
                    self.assertEqual(second, 1)
                finally:
                    if second >= 0:
                        ole32.CoUninitialize()
            except BaseException as error:
                failures.append(error)
            finally:
                if initialized >= 0:
                    ole32.CoUninitialize()

        names = audio.list_output_devices()
        self.assertEqual(names, sorted(set(names)))
        self.assertTrue(all(isinstance(name, str) for name in names))
        thread = threading.Thread(target=enumerate_in_sta)
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive(), 'Core Audio enumeration timed out')
        if failures:
            raise failures[0]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], names)

    def test_hresult_is_signed_32_bit(self):
        self.assertTrue(hasattr(audio, '_check_hresult'), 'HRESULT validation missing')
        for value in (0, 1):
            audio._check_hresult(value, 'operation')
        for value in (0x80004005, -2147467259):
            with self.subTest(value=value):
                with self.assertRaisesRegex(OSError, 'operation.*80004005'):
                    audio._check_hresult(value, 'operation')

    def test_windows_abi_structures(self):
        self.assertTrue(hasattr(audio, '_PROPVARIANT'), 'Core Audio ABI missing')
        self.assertEqual(ctypes.sizeof(audio._GUID), 16)
        self.assertEqual(ctypes.sizeof(audio._PROPERTYKEY), 20)
        self.assertEqual(ctypes.sizeof(audio._PROPVARIANT),
                         24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
        self.assertEqual(audio._PROPVARIANT.value.offset, 8)
        self.assertEqual(audio._PKEY_DEVICE_FRIENDLY_NAME.pid, 14)
        self.assertEqual(bytes(audio._PKEY_DEVICE_FRIENDLY_NAME.fmtid).hex(),
                         '4e255ca41cdf fd4e802067d146a850e0'.replace(' ', ''))

    def test_enumeration_preserves_full_names_sorts_and_deduplicates(self):
        self.assertTrue(hasattr(audio, '_list_windows'), 'Windows enumeration missing')
        names = ['Żółć — ' + 'long device name ' * 12, 'Z speakers', 'A speakers',
                 'Z speakers']
        fake = FakeCoreAudio(names)
        with mock.patch.object(audio, '_com_method', side_effect=fake.method):
            self.assertEqual(audio._list_windows(fake.ole32), sorted(set(names)))
        self.assertEqual(fake.enum_arguments, (0, 1))
        self.assertEqual(fake.access_modes, [0] * len(names))
        self.assertEqual(fake.cleared, len(names))
        self.assertEqual(fake.released, [200, 100, 201, 101, 202, 102, 203, 103, 20, 10])
        fake.ole32.CoUninitialize.assert_called_once_with()

    def test_com_initialization_balanced_even_on_failure(self):
        self.assertTrue(hasattr(audio, '_list_windows'), 'Windows enumeration missing')
        for initialization in (0, 1, 0x80010106, -2147417850, 0x80004005):
            with self.subTest(initialization=initialization):
                fake = FakeCoreAudio(['speakers'], initialization, fail_get=True)
                with mock.patch.object(audio, '_com_method', side_effect=fake.method):
                    with self.assertRaises(OSError):
                        audio._list_windows(fake.ole32)
                self.assertEqual(fake.ole32.CoUninitialize.call_count,
                                 int(initialization in (0, 1)))
                if initialization != 0x80004005:
                    self.assertEqual(fake.cleared, 1)
                    self.assertEqual(fake.released, [200, 100, 20, 10])
                else:
                    fake.ole32.CoCreateInstance.assert_not_called()

    def test_empty_endpoints_is_empty_not_a_fabricated_default(self):
        self.assertTrue(hasattr(audio, '_list_windows'), 'Windows enumeration missing')
        fake = FakeCoreAudio([])
        with mock.patch.object(audio, '_com_method', side_effect=fake.method):
            self.assertEqual(audio._list_windows(fake.ole32), [])
        self.assertEqual(fake.released, [20, 10])

    def test_invalid_name_variant_fails_and_releases_resources(self):
        self.assertTrue(hasattr(audio, '_list_windows'), 'Windows enumeration missing')
        for variant_type, text in ((0, 'name'), (31, None)):
            fake = FakeCoreAudio([text], variant_type=variant_type)
            with mock.patch.object(audio, '_com_method', side_effect=fake.method):
                with self.assertRaises(OSError):
                    audio._list_windows(fake.ole32)
            self.assertEqual(fake.cleared, 1)
            self.assertEqual(fake.released, [200, 100, 20, 10])


class FakeCoreAudio:
    """Only the OS boundary is faked; ABI/name/error/cleanup logic runs."""
    def __init__(self, names, initialization=0, fail_get=False, variant_type=31):
        self.names = names
        self.fail_get = fail_get
        self.variant_type = variant_type
        self.released = []
        self.cleared = 0
        self.enum_arguments = None
        self.access_modes = []
        self.buffers = []
        self.ole32 = mock.Mock()
        self.ole32.CoInitializeEx.return_value = initialization
        self.ole32.CoCreateInstance.side_effect = self.create
        self.ole32.PropVariantClear.side_effect = self.clear

    def create(self, clsid, outer, context, iid, result):
        result._obj.value = 10
        return 0

    def clear(self, value):
        self.cleared += 1
        return 0

    def method(self, pointer, index, result_type, *argtypes):
        def call(*args):
            address = pointer.value
            if index == 2:
                self.released.append(address)
                return 0
            if address == 10 and index == 3:
                self.enum_arguments = args[:2]
                args[2]._obj.value = 20
            elif address == 20 and index == 3:
                args[0]._obj.value = len(self.names)
            elif address == 20 and index == 4:
                args[1]._obj.value = 100 + args[0]
            elif 100 <= address < 200 and index == 4:
                self.access_modes.append(args[0])
                args[1]._obj.value = address + 100
            elif address >= 200 and index == 5:
                if self.fail_get:
                    return 0x80004005
                self_variant = args[1]._obj
                self_variant.vt = self.variant_type
                name = self.names[address - 200]
                if name is not None:
                    buffer = ctypes.create_unicode_buffer(name)
                    self.buffers.append(buffer)
                    self_variant.value.pwszVal = ctypes.cast(buffer, ctypes.c_wchar_p)
            else:
                raise AssertionError((address, index, args))
            return 0
        return call


if __name__ == '__main__':
    unittest.main()
