import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '92fe9bcc1145b535b5da7b33a3bfef70b22225f10e48b21abeae58507e6aca42'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import page
source = iter(range(10))
assert page(source,2,3) == [2,3,4] and next(source) == 5
source = iter([1,2])
assert page(source,100,0) == [] and next(source) == 1
assert page(iter([1]),8,2) == []
for args in [(True,2),(0,False),(-1,2),(0,1.5)]:
    try:
        page([], *args)
    except ValueError:
        pass
    else:
        raise AssertionError('bounds')
