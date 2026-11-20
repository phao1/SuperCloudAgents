import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'bf739f7736e7c98561a87460bf8f1bacd1c4f60d40d5ccc5cf269e07a64c5cbb'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
assert Path('app.py').read_text() == "def windows(values, width):\n    if width <= 0:\n        raise ValueError('width')\n    values = list(values)\n    return [tuple(values[i:i+width]) for i in range(len(values)-width+1)]\n"

import os
import shutil
import tempfile

def run_tests(directory):
    result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'], cwd=directory, capture_output=True, text=True, timeout=15, env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'})
    assert 'Ran 0 tests' not in result.stderr, 'no tests discovered'
    return result.returncode

with tempfile.TemporaryDirectory() as scratch:
    root = Path(scratch)
    shutil.copytree('tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    (root / 'app.py').write_text(Path('app.py').read_text())
    assert run_tests(root) == 0, 'tests fail correct implementation'
    (root / 'app.py').write_text('def windows(values,width):\n    values=list(values)\n    return [tuple(values[i:i+width]) for i in range(0,len(values),width)]\n')
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
    (root / 'app.py').write_text('def windows(values,width):\n    values=list(values)\n    return [tuple(values[i:i+width]) for i in range(len(values))]\n')
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
    (root / 'app.py').write_text('def windows(values,width):\n    if width <= 0: return []\n    values=list(values)\n    return [tuple(values[i:i+width]) for i in range(len(values)-width+1)]\n')
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
