import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'cccd4ebd85c1064d6cb5ce38102f9be8c3250d309673b7e9d4f638d05cc064d9'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
lines = Path('app.py').read_bytes().splitlines(keepends=True)
assert hashlib.sha256(b''.join(lines[:704] + lines[705:])).hexdigest() == '5e1934df1a0eb5a998a1764b933126f93419055e11bf1cc3622a655ed7252715'
from app import lookup
x = lookup('service-0702')
x['timeout'] = 999
assert lookup('service-0702')['timeout'] != 999
assert lookup('service-0702') == {'timeout': 30, 'retries': 0, 'label': '服务0702'}
