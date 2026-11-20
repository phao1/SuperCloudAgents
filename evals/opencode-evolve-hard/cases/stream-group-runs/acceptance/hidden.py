import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': 'f969f908472c3f8fa52379834aef6ece58a3de0ed86d42b9470799e989100611'}
for name, digest in PROTECTED.items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest, name
subprocess.run([sys.executable, '-B', 'verify.py'], check=True, timeout=8)

def rejects(call, message=None):
    try:
        call()
    except ValueError as error:
        if message is not None:
            assert str(error) == message, (str(error), message)
    else:
        raise AssertionError('expected ValueError')

from app import runs
from itertools import count, islice
assert list(runs([])) == []
assert list(runs(iter([None,None,1,None]))) == [(None,(None,None)),(1,(1,)),(None,(None,))]
a,b,c = [{'k':[1]},{'k':[1]},{'k':[2]}]
out = list(runs([a,b,c],lambda x:x['k']))
assert out == [([1],(a,b)),([2],(c,))] and out[0][1][0] is a
consumed, keyed = [], []
def source():
    for n in [1,1,2,3]:
        consumed.append(n)
        yield n
def key(n):
    keyed.append(n)
    return n
r = runs(source(),key)
assert consumed == keyed == []
assert next(r) == (1,(1,1))
assert consumed == keyed == [1,1,2]
assert next(r) == (2,(2,))
assert consumed == keyed == [1,1,2,3]
assert list(r) == [(3,(3,))]
assert list(islice(runs(count()),4)) == [(n,(n,)) for n in range(4)]
error = RuntimeError('sentinel')
def fail():
    yield 1
    raise error
try:
    next(runs(fail()))
except RuntimeError as caught:
    assert caught is error
else:
    raise AssertionError('source failure lost')
def badkey(x):
    raise error
try:
    next(runs([1],badkey))
except RuntimeError as caught:
    assert caught is error
else:
    raise AssertionError('key failure lost')
