import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '64045bfb45d0992d4f3c09fe755866e0db89960ff885e283df0685b7fb5fa6f0'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
lines = Path('app.py').read_bytes().splitlines(keepends=True)
assert hashlib.sha256(b''.join(lines[:419] + lines[420:])).hexdigest() == '07bbd3bf3d9092e628d93c9af1a9b88b04f77b63e3adf1294ba68b3c91e3129e'
from app import lookup
x = lookup('service-0417')
x['timeout'] = 999
assert lookup('service-0417')['timeout'] != 999
assert lookup('service-0417') == {'timeout': 0, 'retries': 3, 'label': '服务0417'}
