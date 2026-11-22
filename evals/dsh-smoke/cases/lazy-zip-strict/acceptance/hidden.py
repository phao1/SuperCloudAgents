import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '842fe2bbf1211d646c4125fba80ef6edbd8d59b7227c1f09b3ddf241d03e13bf'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import pairs
from itertools import count,islice
assert list(pairs([],[])) == []
assert list(pairs([None],[None])) == [(None,None)]
assert list(islice(pairs(count(),count(5)),3)) == [(0,5),(1,6),(2,7)]
left,right = iter(range(4)),iter(range(8))
output = pairs(left,right)
assert next(output) == (0,0)
assert next(left) == 1 and next(right) == 1
output = pairs(iter([1]),iter([2,3]))
assert next(output) == (1,2)
try:
    next(output)
except ValueError:
    pass
else:
    raise AssertionError('late mismatch')
