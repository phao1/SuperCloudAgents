import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '92885876030594bee65c763b4908f4a65080b30fab88842bcea6d228a4783b43'}
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

from app import merge
from itertools import count,islice
assert list(merge([],[])) == []
assert list(merge([],iter([1,2]))) == [1,2]
left=[(1,'a'),(1,'b'),(3,'c')]; right=[(1,'d'),(2,'e')]
assert list(merge(iter(left),iter(right),lambda x:x[0])) == [(1,'a'),(1,'b'),(1,'d'),(2,'e'),(3,'c')]
assert left == [(1,'a'),(1,'b'),(3,'c')]
assert list(merge([None],[None],lambda x:0)) == [None,None]
seen, keyed = [], []
def source(tag, values):
    for n in values:
        seen.append((tag,n))
        yield n
def key(n):
    keyed.append(n)
    return n
m=merge(source('l',[1,3]),source('r',[2,4]),key)
assert seen == keyed == []
assert next(m)==1
assert seen==[('l',1),('r',2)] and keyed==[1,2]
assert next(m)==2
assert seen==[('l',1),('r',2),('l',3)] and keyed==[1,2,3]
assert list(m)==[3,4] and keyed==[1,2,3,4]
assert list(islice(merge(count(0,2),count(1,2)),20)) == list(range(20))
class Once:
    def __init__(self): self.done=False
    def __iter__(self): return self
    def __next__(self):
        assert not self.done, 'exhausted source advanced again'
        self.done=True
        raise StopIteration
assert list(merge(Once(),[1,2,3]))==[1,2,3]
error=RuntimeError('source')
def fail():
    raise error
    yield
try: next(merge(fail(),[]))
except RuntimeError as caught: assert caught is error
else: raise AssertionError('lost failure')
