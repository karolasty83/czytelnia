"""Preferences tests only use files under the project's .test-work."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
MODULE = PROJECT / "addon/globalPlugins/czytelnia/preferences.py"


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "Preferences module is missing")
        spec = importlib.util.spec_from_file_location("czytelnia_preferences_test", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        root = PROJECT / ".test-work"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, prefix="preferences-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "czytelnia-ustawienia.json"

    def test_output_defaults_missing_and_legacy_without_writes(self):
        preferences = self.module.Preferences(self.path)
        self.assertEqual(getattr(preferences, "output_device", None), "")
        self.assertFalse(self.path.exists())
        original = b'{"version":1,"auto_next":false,"volume":37,"speed":1.5}\n'
        self.path.write_bytes(original)
        preferences = self.module.Preferences(self.path)
        self.assertEqual(preferences.output_device, "")
        self.assertEqual(self.path.read_bytes(), original)

    def test_output_round_trip_preserves_other_fields_and_resets_to_default(self):
        preferences = self.module.Preferences(self.path)
        self.assertTrue(callable(getattr(preferences, "set_output_device", None)))
        preferences.set_auto_next(False)
        preferences.set_automatic_messages(False)
        preferences.set_volume(37)
        preferences.set_speed(1.5)
        for name in ("USB Speakers (SDL)", "Słuchawki 🎧", "x" * 1024, ""):
            with self.subTest(name=name):
                preferences.set_output_device(name)
                loaded = self.module.Preferences(self.path)
                self.assertEqual(loaded.output_device, name)
                self.assertEqual(preferences.output_device, name)
                self.assertEqual(json.loads(self.path.read_text()), {
                    "version": 1, "auto_next": False, "automatic_messages": False,
                    "volume": 37, "speed": 1.5, "output_device": name})

    def test_output_setter_rejects_invalid_values_without_disk_or_cache_changes(self):
        preferences = self.module.Preferences(self.path)
        preferences.set_output_device("Speakers")
        original = self.path.read_bytes()
        for value in (None, True, 1, 1.5, [], {}, b"Speakers", "bad\x00name", "x" * 1025):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    preferences.set_output_device(value)
                self.assertEqual(preferences.output_device, "Speakers")
                self.assertEqual(self.path.read_bytes(), original)

    def test_output_invalid_documents_fail_without_reset_or_overwrite(self):
        preferences = self.module.Preferences(self.path)
        for value in (None, True, 1, 1.5, [], {}, "bad\x00name", "x" * 1025):
            with self.subTest(value=value):
                original = json.dumps({"version": 1, "auto_next": True,
                                       "output_device": value}).encode()
                self.path.write_bytes(original)
                with self.assertRaises(self.module.PreferencesError):
                    self.module.Preferences(self.path)
                for field, valid in (("auto_next", False), ("automatic_messages", False),
                                     ("volume", 37), ("speed", 1.5),
                                     ("output_device", "Speakers")):
                    with self.assertRaises(self.module.PreferencesError):
                        getattr(preferences, "set_" + field)(valid)
                    self.assertEqual(preferences.output_device, "")
                    self.assertEqual(self.path.read_bytes(), original)

    def test_output_deleted_document_cannot_resurrect_cached_selection(self):
        for field, value in (("auto_next", False), ("automatic_messages", False),
                             ("volume", 37), ("speed", 1.5), ("output_device", "")):
            with self.subTest(field=field):
                preferences = self.module.Preferences(self.path)
                preferences.set_output_device("Old Speakers")
                self.path.unlink()
                self.assertEqual(self.module.Preferences(self.path).output_device, "")
                self.assertFalse(self.path.exists())
                getattr(preferences, "set_" + field)(value)
                self.assertEqual(preferences.output_device, "")
                self.assertEqual(json.loads(self.path.read_text())["output_device"], "")

    def test_output_failed_save_preserves_cached_selection(self):
        preferences = self.module.Preferences(self.path)
        preferences.set_output_device("Speakers")
        original = self.path.read_bytes()
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation):
                with mock.patch.object(self.module.os, operation, side_effect=OSError("disk error")):
                    with self.assertRaises(OSError):
                        preferences.set_output_device("")
                self.assertEqual(preferences.output_device, "Speakers")
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_playback_defaults_and_optional_fields_load_without_writes(self):
        preferences = self.module.Preferences(self.path)
        self.assertEqual((preferences.volume, preferences.speed), (100, 1.0))
        self.assertFalse(self.path.exists())
        for optional in ({}, {"automatic_messages": False}, {"volume": 0},
                         {"speed": 1.5}, {"volume": 37, "speed": 2},
                         {"automatic_messages": False, "volume": 100, "speed": 1.0}):
            with self.subTest(optional=optional):
                original = json.dumps({"version": 1, "auto_next": False, **optional}).encode()
                self.path.write_bytes(original)
                loaded = self.module.Preferences(self.path)
                self.assertEqual(loaded.volume, optional.get("volume", 100))
                self.assertEqual(loaded.speed, optional.get("speed", 1.0))
                self.assertIs(type(loaded.speed), float)
                self.assertIs(loaded.auto_next, False)
                self.assertEqual(self.path.read_bytes(), original)

    def test_playback_setters_round_trip_and_preserve_complete_schema(self):
        preferences = self.module.Preferences(self.path)
        preferences.set_auto_next(False)
        preferences.set_automatic_messages(False)
        for volume in (0, 37, 100):
            for speed in (1, 1.5, 2.0):
                with self.subTest(volume=volume, speed=speed):
                    preferences.set_volume(volume)
                    preferences.set_speed(speed)
                    loaded = self.module.Preferences(self.path)
                    self.assertEqual((loaded.volume, loaded.speed), (volume, speed))
                    self.assertEqual(json.loads(self.path.read_text()), {
                        "version": 1, "auto_next": False, "automatic_messages": False,
                        "volume": volume, "speed": float(speed), "output_device": ""})

    def test_every_setter_migrates_legacy_even_when_value_unchanged(self):
        for legacy in ({"version": 1, "auto_next": False},
                       {"version": 1, "auto_next": False, "automatic_messages": False},
                       {"version": 1, "auto_next": False, "volume": 37},
                       {"version": 1, "auto_next": False, "speed": 1.5},
                       {"version": 1, "auto_next": False, "volume": 37, "speed": 1.5},
                       {"version": 1, "auto_next": False, "automatic_messages": False,
                        "volume": 37, "speed": 1.5}):
            for field, value in (("auto_next", False), ("automatic_messages", False),
                                 ("volume", 100), ("speed", 1.0), ("output_device", "")):
                with self.subTest(legacy=legacy, field=field):
                    self.path.write_text(json.dumps(legacy))
                    preferences = self.module.Preferences(self.path)
                    getattr(preferences, "set_" + field)(value)
                    expected = {"version": 1, "auto_next": False,
                                "automatic_messages": legacy.get("automatic_messages", True),
                                "volume": legacy.get("volume", 100),
                                "speed": legacy.get("speed", 1.0), "output_device": "", field: value}
                    self.assertEqual(json.loads(self.path.read_text()), expected)

    def test_playback_setters_reject_invalid_values_without_changes(self):
        preferences = self.module.Preferences(self.path)
        preferences.set_volume(37)
        preferences.set_speed(1.5)
        original = self.path.read_bytes()
        for field, values in (
                ("volume", (-1, 101, 50.0, True, False, None, "50", [], {})),
                ("speed", (0, 1.25, 3, True, False, None, "1.5", [], {},
                           float("nan"), float("inf")))):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises((TypeError, ValueError)):
                        getattr(preferences, "set_" + field)(value)
                    self.assertEqual((preferences.volume, preferences.speed), (37, 1.5))
                    self.assertEqual(self.path.read_bytes(), original)

    def test_every_setter_preserves_latest_disk_fields_for_stale_instances(self):
        changes = {"auto_next": False, "automatic_messages": False, "volume": 37, "speed": 1.5,
                   "output_device": "USB Speakers"}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.path.unlink(missing_ok=True)
                stale = self.module.Preferences(self.path)
                writer = self.module.Preferences(self.path)
                for other_field, other_value in changes.items():
                    if other_field != field:
                        getattr(writer, "set_" + other_field)(other_value)
                getattr(stale, "set_" + field)(value)
                self.assertEqual(json.loads(self.path.read_text()), {"version": 1, **changes})
                self.assertEqual({name: getattr(stale, name) for name in changes}, changes)

    def test_playback_failed_save_rolls_back_all_cached_fields(self):
        for field, value in (("auto_next", True), ("automatic_messages", True),
                             ("volume", 37), ("speed", 1.5), ("output_device", "New Speakers")):
            for operation in ("replace", "fsync"):
                with self.subTest(field=field, operation=operation):
                    self.path.unlink(missing_ok=True)
                    stale = self.module.Preferences(self.path)
                    writer = self.module.Preferences(self.path)
                    writer.set_auto_next(False)
                    writer.set_automatic_messages(False)
                    writer.set_volume(20)
                    writer.set_speed(2.0)
                    writer.set_output_device("Other Speakers")
                    original = self.path.read_bytes()
                    with mock.patch.object(self.module.os, operation, side_effect=OSError("disk error")):
                        with self.assertRaises(OSError):
                            getattr(stale, "set_" + field)(value)
                    self.assertEqual((stale.auto_next, stale.automatic_messages,
                                      stale.volume, stale.speed, stale.output_device),
                                     (True, True, 100, 1.0, ""))
                    self.assertEqual(self.path.read_bytes(), original)
                    self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_unchanged_complete_document_does_not_write_and_refreshes_cache(self):
        values = {"auto_next": False, "automatic_messages": False, "volume": 37, "speed": 1.5,
                  "output_device": "USB Speakers"}
        for field, value in values.items():
            with self.subTest(field=field):
                self.path.unlink(missing_ok=True)
                stale = self.module.Preferences(self.path)
                original = (json.dumps({"version": 1, **values}, indent=4) + "\n").encode()
                self.path.write_bytes(original)
                with mock.patch.object(self.module.tempfile, "NamedTemporaryFile",
                                       side_effect=AssertionError("unexpected write")):
                    getattr(stale, "set_" + field)(value)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual({name: getattr(stale, name) for name in values}, values)

    def test_setter_compares_latest_disk_value_not_cached_value(self):
        defaults = {"auto_next": True, "automatic_messages": True, "volume": 100, "speed": 1.0,
                    "output_device": ""}
        changed = {"auto_next": False, "automatic_messages": False, "volume": 37, "speed": 1.5,
                   "output_device": "Speakers"}
        for field, value in defaults.items():
            with self.subTest(field=field):
                self.path.unlink(missing_ok=True)
                stale = self.module.Preferences(self.path)
                writer = self.module.Preferences(self.path)
                getattr(writer, "set_" + field)(changed[field])
                getattr(stale, "set_" + field)(value)
                self.assertEqual(getattr(self.module.Preferences(self.path), field), value)

    def test_default_setter_creates_complete_missing_file(self):
        for field, value in (("auto_next", True), ("automatic_messages", True),
                             ("volume", 100), ("speed", 1.0), ("output_device", "")):
            with self.subTest(field=field):
                self.path.unlink(missing_ok=True)
                preferences = self.module.Preferences(self.path)
                getattr(preferences, "set_" + field)(value)
                self.assertEqual(json.loads(self.path.read_text()), {
                    "version": 1, "auto_next": True, "automatic_messages": True,
                    "volume": 100, "speed": 1.0, "output_device": ""})

    def test_concurrent_setters_serialize_read_modify_write_and_cache(self):
        self._assert_concurrent_setters("volume", 37)

    def test_output_setter_shares_read_save_cache_lock(self):
        self._assert_concurrent_setters("output_device", "Speakers")

    def _assert_concurrent_setters(self, field, value):
        preferences = self.module.Preferences(self.path)
        preferences.set_auto_next(True)
        first_read = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_read = threading.Event()
        read = preferences._read

        def controlled_read():
            values = read()
            if not first_read.is_set():
                first_read.set()
                if not release_first.wait(5):
                    raise AssertionError("first setter was not released")
            else:
                second_read.set()
            return values

        def second_setter():
            second_started.set()
            preferences.set_auto_next(False)

        with mock.patch.object(preferences, "_read", side_effect=controlled_read):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(getattr(preferences, "set_" + field), value)
                try:
                    self.assertTrue(first_read.wait(5), "first setter did not read")
                    second = executor.submit(second_setter)
                    self.assertTrue(second_started.wait(5), "second setter did not start")
                    self.assertFalse(second_read.wait(0.2), "second setter raced with first read/save")
                finally:
                    release_first.set()
                first.result(timeout=5)
                second.result(timeout=5)
        expected = {
            "version": 1, "auto_next": False, "automatic_messages": True,
            "volume": 100, "speed": 1.0, "output_device": "", field: value}
        self.assertEqual(json.loads(self.path.read_text()), expected)
        self.assertEqual((preferences.auto_next, getattr(preferences, field)), (False, value))

    def test_invalid_playback_documents_are_not_reset(self):
        preferences = self.module.Preferences(self.path)
        for field, values in (
                ("volume", (-1, 101, 50.0, True, False, None, "50", [], {})),
                ("speed", (0, 1.25, 3, True, False, None, "1.5", [], {},
                           float("nan"), float("inf")))):
            for value in values:
                with self.subTest(field=field, value=value):
                    original = json.dumps({"version": 1, "auto_next": True, field: value}).encode()
                    self.path.write_bytes(original)
                    with self.assertRaises(self.module.PreferencesError):
                        self.module.Preferences(self.path)
                    for setter, valid_value in ((preferences.set_auto_next, True),
                                                (preferences.set_automatic_messages, True),
                                                (preferences.set_volume, 100),
                                                (preferences.set_speed, 1.0)):
                        with self.assertRaises(self.module.PreferencesError):
                            setter(valid_value)
                        self.assertEqual((preferences.auto_next, preferences.automatic_messages,
                                          preferences.volume, preferences.speed), (True, True, 100, 1.0))
                    self.assertEqual(self.path.read_bytes(), original)

    def test_default_true_without_constructor_write(self):
        preferences = self.module.Preferences(self.path)
        self.assertIs(preferences.auto_next, True)
        self.assertFalse(self.path.exists())

    def test_automatic_messages_defaults_and_legacy_load_do_not_write(self):
        preferences = self.module.Preferences(self.path)
        self.assertIs(preferences.automatic_messages, True)
        self.assertFalse(self.path.exists())
        original = b'{"version":1,"auto_next":false}\n'
        self.path.write_bytes(original)
        preferences = self.module.Preferences(self.path)
        self.assertIs(preferences.auto_next, False)
        self.assertIs(preferences.automatic_messages, True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_round_trip_strict_boolean_complete_schema_atomic_replace(self):
        preferences = self.module.Preferences(self.path)
        replace = self.module.os.replace
        with mock.patch.object(self.module.os, "replace", wraps=replace) as replaced:
            preferences.set_auto_next(False)
        source, destination = replaced.call_args.args
        self.assertEqual(Path(source).parent, self.path.parent)
        self.assertEqual(Path(destination), self.path)
        self.assertEqual(json.loads(self.path.read_text()),
                         {"version": 1, "auto_next": False, "automatic_messages": True,
                          "volume": 100, "speed": 1.0, "output_device": ""})
        self.assertIs(preferences.auto_next, False)
        self.assertIs(self.module.Preferences(self.path).auto_next, False)
        for value in (0, 1, None, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                preferences.set_auto_next(value)
        self.assertIs(preferences.auto_next, False)
        preferences.set_auto_next(True)
        self.assertIs(self.module.Preferences(self.path).auto_next, True)

    def test_automatic_messages_round_trip_preserves_auto_next(self):
        self.path.write_text('{"version":1,"auto_next":false}', encoding="utf-8")
        preferences = self.module.Preferences(self.path)
        preferences.set_automatic_messages(False)
        self.assertEqual(json.loads(self.path.read_text()),
                         {"version": 1, "auto_next": False, "automatic_messages": False,
                          "volume": 100, "speed": 1.0, "output_device": ""})
        self.assertIs(preferences.automatic_messages, False)
        loaded = self.module.Preferences(self.path)
        self.assertIs(loaded.automatic_messages, False)
        self.assertIs(loaded.auto_next, False)
        loaded.set_auto_next(True)
        self.assertEqual(json.loads(self.path.read_text()),
                         {"version": 1, "auto_next": True, "automatic_messages": False,
                          "volume": 100, "speed": 1.0, "output_device": ""})
        loaded.set_automatic_messages(True)
        self.assertIs(self.module.Preferences(self.path).automatic_messages, True)
        for value in (0, 1, None, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                loaded.set_automatic_messages(value)
        self.assertIs(loaded.automatic_messages, True)

    def test_stale_instances_merge_other_field_from_disk(self):
        for first_field, second_field in (("auto_next", "automatic_messages"),
                                          ("automatic_messages", "auto_next")):
            with self.subTest(first=first_field):
                self.path.unlink(missing_ok=True)
                first = self.module.Preferences(self.path)
                second = self.module.Preferences(self.path)
                getattr(first, "set_" + first_field)(False)
                getattr(second, "set_" + second_field)(False)
                self.assertEqual(json.loads(self.path.read_text()),
                                 {"version": 1, "auto_next": False, "automatic_messages": False,
                                  "volume": 100, "speed": 1.0, "output_device": ""})
                self.assertIs(second.auto_next, False)
                self.assertIs(second.automatic_messages, False)

    def test_invalid_document_explicit_error_never_overwrites(self):
        for text in ('{broken', '[]', '{}', '{"version":2,"auto_next":true}',
                     '{"version":true,"auto_next":true}',
                     '{"version":1,"auto_next":0}',
                     '{"version":1,"auto_next":true,"extra":1}'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(self.module.PreferencesError):
                    self.module.Preferences(self.path)
                self.assertEqual(self.path.read_text(), text)
        self.path.unlink()
        preferences = self.module.Preferences(self.path)
        self.path.write_text('{broken', encoding="utf-8")
        with self.assertRaises(self.module.PreferencesError):
            preferences.set_auto_next(False)
        self.assertEqual(self.path.read_text(), '{broken')
        self.assertIs(preferences.auto_next, True)

    def test_failed_save_retains_memory_file_and_unrelated_files(self):
        preferences = self.module.Preferences(self.path)
        preferences.set_auto_next(False)
        old = self.path.read_bytes()
        unrelated = self.root / "other.tmp"
        unrelated.write_bytes(b"keep")
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation):
                with mock.patch.object(self.module.os, operation, side_effect=OSError("disk error")):
                    with self.assertRaises(OSError):
                        preferences.set_auto_next(True)
                self.assertIs(preferences.auto_next, False)
                self.assertEqual(self.path.read_bytes(), old)
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertEqual(set(self.root.iterdir()), {self.path, unrelated})

    def test_new_field_rejects_malformed_documents_without_reset(self):
        preferences = self.module.Preferences(self.path)
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value):
                original = json.dumps({"version": 1, "auto_next": False,
                                       "automatic_messages": value}).encode("utf-8")
                self.path.write_bytes(original)
                with self.assertRaises(self.module.PreferencesError):
                    self.module.Preferences(self.path)
                for setter in (preferences.set_auto_next, preferences.set_automatic_messages):
                    with self.assertRaises(self.module.PreferencesError):
                        setter(False)
                    self.assertEqual(self.path.read_bytes(), original)
                    self.assertIs(preferences.auto_next, True)
                    self.assertIs(preferences.automatic_messages, True)
        self.path.write_text('{"version":1,"automatic_messages":false}', encoding="utf-8")
        with self.assertRaises(self.module.PreferencesError):
            self.module.Preferences(self.path)

    def test_failed_new_field_save_rolls_back_both_cached_values(self):
        preferences = self.module.Preferences(self.path)
        other = self.module.Preferences(self.path)
        other.set_auto_next(False)
        original = self.path.read_bytes()
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation):
                with mock.patch.object(self.module.os, operation, side_effect=OSError("disk error")):
                    with self.assertRaises(OSError):
                        preferences.set_automatic_messages(False)
                self.assertIs(preferences.auto_next, True)
                self.assertIs(preferences.automatic_messages, True)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_rejects_destination_and_parent_links(self):
        target = self.root / "target.json"
        original = '{"version":1,"auto_next":false}'
        target.write_text(original)
        try:
            self.path.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"Symlink unavailable: {error}")
        with self.assertRaises(self.module.PreferencesError):
            self.module.Preferences(self.path)
        self.path.unlink()
        preferences = self.module.Preferences(self.path)
        self.path.symlink_to(target)
        with self.assertRaises(self.module.PreferencesError):
            preferences.set_auto_next(True)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), original)
        parent = self.root / "linked-parent"
        parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(self.module.PreferencesError):
            self.module.Preferences(parent / "target.json")
        self.path.unlink()
        self.path.symlink_to(self.root / "missing-target")
        with self.assertRaises(self.module.PreferencesError):
            self.module.Preferences(self.path)


if __name__ == "__main__":
    unittest.main()
