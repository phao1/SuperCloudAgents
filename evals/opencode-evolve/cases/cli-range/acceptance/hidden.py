import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'ec7cadfcc114d180c42c79e5822feda8c3ce2d188a74e2c11eb9338d773e2b61'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import render
assert render(5,0,-2) == '5,3,1\n'
assert render(2,2) == '\n'
for args, expected in [(['5','0','--step','-2'],'5,3,1\n'), (['0','5','--step','2'],'0,2,4\n')]:
    r = subprocess.run([sys.executable,'-m','cli',*args],capture_output=True,text=True)
    assert r.returncode == 0 and r.stdout == expected
for args in [['a','2'], ['1'], ['1','4','--step','0'], ['1','4','--bad']]:
    r = subprocess.run([sys.executable,'-m','cli',*args],capture_output=True,text=True)
    assert r.returncode != 0 and r.stdout == ''
try:
    render(1,4,0)
except ValueError:
    pass
else:
    raise AssertionError('zero')
