"""Build a deterministic, stdlib-only judge archive and verify its HTTP entry."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
PACKAGE_DIR = 'CoreGeek'  # Platform starts /home/docker/CoreGeek/main3.py


def build(destination):
    files = {name: ROOT / name for name in ('main3.py', 'run.sh', 'README.md',
                                           'ARCHITECTURE.md', 'VALIDATION.md')}
    files.update({p.relative_to(ROOT).as_posix(): p for p in (ROOT / 'src').rglob('*.py')})
    files.update({p.relative_to(ROOT).as_posix(): p for p in (ROOT / 'reports').glob('*.json')})
    for name in ('任务书.md', '接口文档.md'):
        files['docs/official/' + name] = ROOT.parents[1] / 'docs' / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with destination.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for name, path in sorted(files.items()):
                data = path.read_bytes().replace(b'\r\n', b'\n')
                info = tarfile.TarInfo(PACKAGE_DIR + '/' + name)
                info.size, info.mode, info.mtime = len(data), 0o755 if name == 'run.sh' else 0o644, 0
                archive.addfile(info, io.BytesIO(data))
                manifest[name] = hashlib.sha256(data).hexdigest()
            data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
            info = tarfile.TarInfo(PACKAGE_DIR + '/MANIFEST.sha256.json')
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + '.sha256').write_text(digest + '  ' + destination.name + '\n', encoding='ascii')
    return digest


def verify(destination):
    with tempfile.TemporaryDirectory(prefix='coregeek-v2-verify-') as folder:
        extracted = Path(folder)
        with tarfile.open(destination, 'r:gz') as archive:
            for member in archive.getmembers():
                target = (extracted / member.name).resolve()
                if not target.is_relative_to(extracted.resolve()) or not member.isfile():
                    raise ValueError('Unexpected archive member: ' + member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                assert member.name.startswith(PACKAGE_DIR + '/'), member.name
                if member.name == PACKAGE_DIR + '/run.sh':
                    assert member.mode == 0o755
        app_root = extracted / PACKAGE_DIR
        assert (app_root / 'main3.py').is_file()
        assert not (extracted / 'main3.py').exists()
        for name, digest in json.loads((app_root / 'MANIFEST.sha256.json').read_text(encoding='utf-8')).items():
            assert hashlib.sha256((app_root / name).read_bytes()).hexdigest() == digest, name
        assert b'\r' not in (app_root / 'run.sh').read_bytes()
        sample = (ROOT.parents[1] / 'docs' / 'request.txt').read_text(encoding='utf-8-sig')
        # The checked-in illustrative sample contains a trailing comma.
        payload = json.loads(re.sub(r',(\s*[}\]])', r'\1', sample))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        with (extracted / 'server.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-I', '-B', str(app_root / 'main3.py'), str(port)],
                                       cwd=folder, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                url = f'http://127.0.0.1:{port}/'
                def post(value):
                    request = Request(url, data=json.dumps(value).encode(), headers={'Content-Type':'application/json'})
                    with urlopen(request, timeout=5) as response:
                        assert response.status == 200
                        return json.load(response)
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError('Extracted server exited; ' + (extracted / 'server.log').read_text(errors='replace'))
                    try:
                        first = post(payload)
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    raise RuntimeError('Extracted server did not start')
                assert set(first) == {'roleCommandMap', 'prompt', 'executeCmd'}
                assert isinstance(first['roleCommandMap'], dict)
                assert isinstance(first['prompt'], str) and isinstance(first['executeCmd'], str)
                assert post(payload) == first
                payload['roundNo'] += 1
                assert set(post(payload)) == set(first)
            finally:
                process.terminate()
                process.wait(timeout=10)
        log_text = (extracted / 'server.log').read_text(errors='replace')
        assert 'Traceback' not in log_text and 'DROP ' not in log_text, log_text


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'CoreGeek.tar.gz')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    digest = build(args.output)
    if args.verify:
        verify(args.output)
    print(json.dumps({'archive':str(args.output.resolve()), 'sha256':digest, 'http_verified':args.verify}))
