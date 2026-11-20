import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '4ec063ee3c83bf6c938023f43cbec8571dbd8c563ada656f2f1cebea4b2e618f'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import Cache
now = [10]
c = Cache(lambda: now[0])
for value in [0, False, '', [], None]:
    c.put('x', value, 2)
    assert c.get('x') == value
now[0] = 12
assert c.get('x') is None
c.put('x', 2, 10)
c.put('x', 9, 0)
assert c.get('x') is None
c.put('x', 4, -1)
assert c.get('x') is None
assert Cache(lambda: 0).get('x') is None
