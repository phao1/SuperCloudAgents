import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '848468240a453d25182c2fe5a8957f78b774d043896f6aeea9e8b1c11c8356d1'}
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

from app import windows
from itertools import count,islice
for n in range(10):
    for width in range(1,6):
        for step in range(1,7):
            data=list(range(n))
            assert list(windows(iter(data),width,step)) == [tuple(data[i:i+width]) for i in range(0,n-width+1,step)]
seen=[]
def source():
    for n in count():
        seen.append(n)
        yield n
w=windows(source(),2,5)
assert seen==[]
assert next(w)==(0,1) and seen==[0,1]
assert next(w)==(5,6) and seen==list(range(7))
for bad in [True,False,0,-1,2.0,'2',None]:
    for width,step in [(bad,1),(1,bad)]:
        n=len(seen)
        rejects(lambda:next(windows(source(),width,step)),'invalid window')
        assert len(seen)==n
x=object()
assert list(windows([None,x],2))==[(None,x)]
assert list(islice(windows(count(),3,2),3))==[(0,1,2),(2,3,4),(4,5,6)]
error=RuntimeError('stream')
def fail():
    yield 1
    raise error
try: next(windows(fail(),2))
except RuntimeError as caught: assert caught is error
else: raise AssertionError('lost failure')
