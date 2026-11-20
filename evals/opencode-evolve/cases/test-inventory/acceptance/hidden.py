import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'c647be35b355fee79452b5d8e9a34b6b6077000ff4e9794de09a6ef08822d1d1'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
assert Path('app.py').read_text() == "def reserve(stock, requests):\n    result = dict(stock)\n    for item, quantity in requests.items():\n        if quantity < 0 or quantity > result.get(item, 0):\n            raise ValueError('quantity')\n        result[item] = result.get(item, 0) - quantity\n    return result\n"

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
    (root / 'app.py').write_text('def reserve(stock,requests):\n    for k,q in requests.items():\n        if q<0 or q>stock.get(k,0): raise ValueError()\n        stock[k]=stock.get(k,0)-q\n    return stock\n')
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
    (root / 'app.py').write_text('def reserve(stock,requests):\n    result=dict(stock)\n    for k,q in requests.items():\n        if q>result.get(k,0): raise ValueError()\n        result[k]=result.get(k,0)-q\n    return result\n')
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
    (root / 'app.py').write_text('def reserve(stock,requests):\n    result=dict(stock)\n    for k,q in requests.items():\n        if q<0: raise ValueError()\n        result[k]=max(0,result.get(k,0)-q)\n    return result\n')
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
