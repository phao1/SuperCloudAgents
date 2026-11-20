import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'd326892874c5d9cdfc97afa5aa5f4a2e86a0a012fecabb60c8d906e372c4ceb6'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import batches
from itertools import count, islice
assert list(batches([], 3)) == []
assert list(islice(batches(count(), 2), 3)) == [(0,1),(2,3),(4,5)]
source = iter(range(9))
output = batches(source, 3)
assert next(output) == (0,1,2) and next(source) == 3
for size in [True, 0, -1, 1.5, '2']:
    try:
        next(batches([], size))
    except ValueError:
        pass
    else:
        raise AssertionError('invalid size')
