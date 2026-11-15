"""Trusted verification script executed only inside the sandbox."""

SCRIPT = r"""
import difflib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

root = pathlib.Path('/workspace')
def git(*args, cwd=root):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null',
        '-c', 'core.fsmonitor=false', *args], cwd=cwd)

def report_checks():
    report = root / 'output/report.md'
    assert report.is_file() and report.stat().st_size, 'output/report.md is missing or empty'
    text = report.read_text()
    references = dict(re.findall(r'^\s*\[([^\]]+)\]:\s*<?([^\s>]+)>?', text, re.M))
    images = re.findall(r'!\[[^\]]*\]\(\s*<?([^\s)>]+)>?', text)
    for label, reference in re.findall(r'!\[([^\]]*)\]\[([^\]]*)\]', text):
        key = reference or label
        assert key in references, 'Undefined image reference: ' + key
        images.append(references[key])
    images += re.findall(r'<img\b[^>]*\bsrc=[\"\x27]([^\"\x27]+)', text, re.I)
    for image in images:
        parsed = urlsplit(image)
        assert not parsed.scheme and not parsed.netloc, 'Image must be local: ' + image
        path = (report.parent / unquote(parsed.path)).resolve()
        assert path.is_relative_to(root / 'output') and path.is_file(), 'Missing image: ' + image
    json.loads((root / 'output/results.json').read_text())

try:
    report_checks()
    base = sys.argv[1]
    assert not git('diff', '--no-ext-diff', '--no-textconv', base, 'HEAD', '--', 'data/'), 'data/ changed from baseline'
    assert not git('status', '--porcelain', '--untracked-files=all', '--', 'data/'), 'data/ has uncommitted changes'
    with tempfile.TemporaryDirectory(prefix='sca-repro-') as temp:
        repro = pathlib.Path(temp) / 'repo'
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-local',
            '--no-checkout', '--', str(root), str(repro)], check=True)
        subprocess.run(['git', '-C', str(repro), '-c', 'core.hooksPath=/dev/null',
            'checkout', '--detach', git('rev-parse', 'HEAD').decode().strip()], check=True)
        expected = {name: (repro / 'output' / name).read_bytes()
            for name in ('report.md', 'results.json')}
        shutil.rmtree(repro / 'output')
        subprocess.run([sys.executable, 'analysis.py'], cwd=repro, check=True)
        failed = False
        for name, before in expected.items():
            after = (repro / 'output' / name).read_bytes()
            if before != after:
                failed = True
                print('Reproducibility mismatch: ' + name)
                print(''.join(difflib.unified_diff(before.decode(errors='replace').splitlines(True),
                    after.decode(errors='replace').splitlines(True),
                    fromfile='committed/' + name, tofile='reproduced/' + name)))
        assert not failed, 'Outputs are not reproducible'
except Exception as exc:
    print(str(exc))
    sys.exit(1)
"""
