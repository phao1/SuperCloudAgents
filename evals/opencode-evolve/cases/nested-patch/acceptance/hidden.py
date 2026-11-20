import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'd163a679f18a0414a443b04c5f4de690e3606017cfc0e83ac943fe9480a75a97'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import patch
from copy import deepcopy
base = {'nested':{'a':[1], 'b':2}, 'keep':[3]}
delta = {'nested':{'b':None,'c':[4]}, 'missing':None}
before = deepcopy((base, delta))
out = patch(base, delta)
assert out == {'nested':{'a':[1], 'c':[4]}, 'keep':[3]}
out['nested']['a'].append(9)
out['nested']['c'].append(8)
out['keep'].clear()
assert (base, delta) == before
assert patch([1], {'a':{'b':2}}) == {'a':{'b':2}}
assert patch({'a':1}, None) is None
assert patch(0, [1,2]) == [1,2]
