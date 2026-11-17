import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class BuildTests(unittest.TestCase):
    def setUp(self):
        target=ROOT/'build.py'
        self.assertTrue(target.exists(),'Brak skryptu budowania')
        spec=importlib.util.spec_from_file_location('build_test_module',target)
        self.module=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
    def test_output_enumerator_is_packaged(self):
        self.assertIn('globalPlugins/czytelnia/audio_outputs.py', self.module.SOURCES)

    def test_local_payload_includes_help_and_keeps_tools(self):
        names = {name for _, name in self.module.payload(ROOT)}
        for name in ('COPYING-GPLv3.txt', 'THIRD-PARTY.txt',
                     'tools/7zip-docs/License.txt', 'tools/7zip-docs/readme.txt'):
            self.assertNotIn(name, names)
        self.assertIn('doc/pl/readme.html', names)
        fields = self.module.validate_manifest((ROOT / 'addon/manifest.ini').read_text(encoding='utf-8'))
        self.assertEqual(fields['docFileName'], 'readme.html')
        document = dict((name, path) for path, name in self.module.payload(ROOT))['doc/pl/readme.html']
        self.assertEqual(document.read_bytes(), (ROOT / 'Czytelnia-podrecznik.html').read_bytes())
        self.assertIn('tools/7z.exe', names)
        self.assertIn('tools/7z.dll', names)

    def test_manifest_allows_no_documentation_reference(self):
        text = (ROOT / 'addon/manifest.ini').read_text(encoding='utf-8')
        text = '\n'.join(line for line in text.splitlines() if not line.startswith('docFileName'))
        fields = self.module.validate_manifest(text)
        self.assertNotIn('docFileName', fields)

    def test_manifest_rejects_section_and_missing_fields(self):
        with self.assertRaises(ValueError): self.module.validate_manifest('[addon]\nname=x')
        with self.assertRaises(ValueError): self.module.validate_manifest('name=x')
    def test_removed_settings_panel_is_not_packaged(self):
        removed = 'globalPlugins/czytelnia/settings_panel.py'
        self.assertNotIn(removed, self.module.SOURCES)
        self.assertNotIn(removed, [name for path, name in self.module.payload(ROOT)])
        self.assertIn('globalPlugins/czytelnia/preferences.py', self.module.SOURCES)

    def test_native_transport_is_packaged_without_probes_or_media(self):
        names = [name for _, name in self.module.payload(ROOT)]
        self.assertIn('globalPlugins/czytelnia/native_seek.py', names)
        self.assertFalse(any('probe' in name and name != 'tools/ffprobe.exe' for name in names))
        self.assertFalse(any(name.endswith(('.mp3', '.wav', '.flac', '.cmd')) for name in names))

    def test_error_log_module_packaged_but_no_generated_logs(self):
        names = [name for _, name in self.module.payload(ROOT)]
        self.assertIn('globalPlugins/czytelnia/error_log.py', names)
        self.assertFalse(any(name.endswith(('.log', '.log.1')) for name in names))

    def test_only_program_files_are_packaged(self):
        files=self.module.payload(ROOT)
        names=[name for path,name in files]
        self.assertIn('tools/ffplay.exe',names)
        self.assertIn('tools/ffprobe.exe',names)
        self.assertFalse(any(name.startswith('bin/') for name in names))
        self.assertIn('globalPlugins/czytelnia/__init__.py',names)
        self.assertIn('globalPlugins/czytelnia/time_text.py', names)
        self.assertFalse(any(name.endswith('.zip') or name.endswith('.json') or '__pycache__' in name for name in names))

if __name__=='__main__': unittest.main()
