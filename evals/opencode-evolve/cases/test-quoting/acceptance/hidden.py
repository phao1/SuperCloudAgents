import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '413002be1f33bf1bcbafa6a9672397a25c739796b922aef728dca5059228deba'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
assert Path('app.py').read_text() == "import csv\nimport io\ndef row(fields):\n    output = io.StringIO(newline='')\n    csv.writer(output).writerow(fields)\n    return output.getvalue()\n"

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
    (root / 'app.py').write_text("def row(fields):\n    return ','.join(fields) + '\\r\\n'\n")
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
    (root / 'app.py').write_text("import csv, io\ndef row(fields):\n    o=io.StringIO(newline='')\n    csv.writer(o,lineterminator='\\n').writerow(fields)\n    return o.getvalue()\n")
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
    (root / 'app.py').write_text("import csv, io\ndef row(fields):\n    o=io.StringIO(newline='')\n    csv.writer(o).writerow([f.encode('ascii','ignore').decode() for f in fields])\n    return o.getvalue()\n")
    shutil.rmtree(root / '__pycache__', ignore_errors=True)
    assert run_tests(root) != 0, 'surviving mutant'
