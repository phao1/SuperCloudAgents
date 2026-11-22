import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '0b5974d03d0f977d800ff83f0ae544f78da2fc10c1c0d7745c9f339a6bf2013a'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import merge
assert merge(iter([(5, 8), (1, 4), (4, 5), (2, 3), (7, 7)])) == [(1, 8)]
assert merge([]) == []
source = [(-4, -2), (0, 1), (-3, 0)]
before = source[:]
assert merge(source) == [(-4, 1)] and source == before
try:
    merge([(3, 2)])
except ValueError:
    pass
else:
    raise AssertionError('reversed interval accepted')
