import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '6245a1ec1ea7238b6b36ad7a9b3daa2f7c97857d7a4438eae2e2b737efc70da5'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
lines = Path('app.py').read_bytes().splitlines(keepends=True)
assert hashlib.sha256(b''.join(lines[:235] + lines[236:])).hexdigest() == '954902ba9868373a10fa1c14924b39dad0e79e4b4509d1ae3c814c1b40c3a489'
from app import lookup
x = lookup('service-0233')
x['timeout'] = 999
assert lookup('service-0233')['timeout'] != 999
assert lookup('service-0233') == {'timeout': 30, 'retries': 3, 'label': '中文「归档」🙂'}
