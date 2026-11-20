import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'f3a350a5bd8df40fc29ef929b59fcc8359ae2c2affd4ce625e2a307ec076c66f'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import describe
for filename, mime in [('a.TAR.GZ','application/gzip'),('a.b.txt','text/plain'),('中文.SVG','image/svg+xml'),('README','application/octet-stream'),('a.unknown','application/octet-stream')]:
    assert describe(filename) == mime
from assets.catalog import TYPES
assert TYPES == {'.json':'application/json','.txt':'text/plain','.svg':'image/svg+xml','.tar.gz':'application/gzip'}
TYPES['.foo'] = 'custom/test'
assert describe('a.FOO') == 'custom/test'
