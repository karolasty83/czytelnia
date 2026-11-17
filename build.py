"""Build a local test add-on. Never installs it; explicit payload excludes books."""
from pathlib import Path
import ast
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCES = (
    'manifest.ini',
    'globalPlugins/czytelnia/__init__.py',
    'globalPlugins/czytelnia/library.py',
    'globalPlugins/czytelnia/numbering.py',
    'globalPlugins/czytelnia/sevenzip.py',
    'globalPlugins/czytelnia/error_log.py',
    'globalPlugins/czytelnia/player.py',
    'globalPlugins/czytelnia/audio_outputs.py',
    'globalPlugins/czytelnia/zmq_control.py',
    'globalPlugins/czytelnia/native_seek.py',
    'globalPlugins/czytelnia/controller.py',
    'globalPlugins/czytelnia/dialogs.py',
    'globalPlugins/czytelnia/explorer.py',
    'globalPlugins/czytelnia/preferences.py',
    'globalPlugins/czytelnia/sleep_timer.py',
    'globalPlugins/czytelnia/time_text.py',
    'globalPlugins/czytelnia/power.py',
)


def validate_manifest(text):
    fields = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'([A-Za-z]+)\s*=\s*(.*?)\s*', line)
        if not match:
            raise ValueError('Niepoprawny manifest: ' + line)
        key, value = match.groups()
        if key in fields:
            raise ValueError('Powtórzony klucz: ' + key)
        fields[key] = value.strip('"')
    required = {'name','summary','description','author','version','minimumNVDAVersion','lastTestedNVDAVersion'}
    if required - fields.keys():
        raise ValueError('Brak pól manifestu: ' + ', '.join(sorted(required-fields.keys())))
    for key in ('version','minimumNVDAVersion','lastTestedNVDAVersion'):
        if not re.fullmatch(r'\d+(?:\.\d+){1,2}', fields[key]):
            raise ValueError('Niepoprawna wersja: ' + key)
    def version(s): return tuple((list(map(int,s.split('.'))) + [0,0])[:3])
    if version(fields['minimumNVDAVersion']) > version(fields['lastTestedNVDAVersion']):
        raise ValueError('Odwrócony zakres wersji NVDA')
    return fields


def payload(root):
    return [(root/'addon'/rel,rel) for rel in SOURCES] + [
        (root/'ffplay.exe','tools/ffplay.exe'), (root/'ffprobe.exe','tools/ffprobe.exe'),
        (root/'tools/7z.exe', 'tools/7z.exe'),
        (root/'tools/7z.dll', 'tools/7z.dll'),
        (root/'Czytelnia-podrecznik.html', 'doc/pl/readme.html')]


def digest(path):
    sha=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            sha.update(block)
    return sha.hexdigest()


def build(root=ROOT):
    files=payload(root)
    for path,name in files:
        if not path.is_file():
            raise FileNotFoundError(path)
        if name.endswith('.py'):
            ast.parse(path.read_text(encoding='utf-8'),filename=name)
        if name.endswith('.exe'):
            with path.open('rb') as stream:
                if stream.read(2) != b'MZ' or path.stat().st_size < 100000:
                    raise ValueError('Nieprawidłowy plik EXE: '+name)
    manifest=validate_manifest((root/'addon/manifest.ini').read_text(encoding='utf-8'))
    if manifest.get('docFileName') and 'doc/pl/'+manifest['docFileName'] not in {name for _, name in files}:
        raise ValueError('Brak pomocy w paczce')
    target=root/(manifest['name']+'-'+manifest['version']+'.nvda-addon')
    temporary=target.with_suffix('.building')
    expected={name:digest(path) for path,name in files}
    try:
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for path,name in files:
                archive.write(path,name)
        with zipfile.ZipFile(temporary) as archive:
            if set(archive.namelist()) != set(expected):
                raise ValueError('Zła zawartość paczki')
            for name,sha in expected.items():
                actual=hashlib.sha256()
                with archive.open(name) as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):
                        actual.update(block)
                if actual.hexdigest() != sha:
                    raise ValueError('Niezgodna suma: '+name)
        temporary.replace(target)
    finally:
        if temporary.exists(): temporary.unlink()
    report={'package':str(target),'bytes':target.stat().st_size,'sha256':digest(target),
            'files':expected,'installed':False}
    (root/'build-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return target


if __name__=='__main__': build()
