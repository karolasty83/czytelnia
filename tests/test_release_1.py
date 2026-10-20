"""Version 1.01 documentation contract; local files only."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip = True
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = False
    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())

class ReleaseTests(unittest.TestCase):
    def test_manifest_version_and_author(self):
        text = (ROOT / 'addon/manifest.ini').read_text(encoding='utf-8')
        fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        fields = {k.strip():v.strip().strip('"') for k,v in fields.items()}
        self.assertEqual(fields['version'], '1.01')
        self.assertEqual(fields['author'], 'Karol Olejnik')
        self.assertEqual(fields['docFileName'], 'readme.html')

    def test_manual_uses_wstep_and_ends_with_author_without_timer_advice(self):
        html = (ROOT / 'Czytelnia-podrecznik.html').read_text(encoding='utf-8')
        parser = Text()
        parser.feed(html)
        text = ' '.join(parser.parts)
        self.assertEqual(parser.parts[-1], 'Karol Olejnik')
        self.assertIn('„Wstęp”', text)
        self.assertNotIn('Zapowiedź', text)
        timer = text.split('Wyłącznik czasowy')[-1]
        self.assertNotIn('innych programach', timer)
        self.assertNotIn('zapisać pracę', timer)
        self.assertNotIn('Narzędzia NVDA', text)
        self.assertIn('menadżera dodatków NVDA', text)

if __name__ == '__main__':
    unittest.main()
