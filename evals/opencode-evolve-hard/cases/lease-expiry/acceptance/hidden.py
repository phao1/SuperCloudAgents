import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '8cb6f4ed8bcf135bd6901c8de71df6a7bc46b564b597e36849f53b8d657fd33c'}
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

from app import Leases
now, calls = [0], [0]
def clock():
    calls[0] += 1
    return now[0]
l = Leases(clock)
for method in [l.acquire,l.renew]:
    for bad in [0,-1,True,False,1.0,'2',None]:
        n = calls[0]
        rejects(lambda: method('k','a',bad),'invalid ttl')
        assert calls[0] == n
assert l.acquire('k','',5) is True
assert l.owner('k') == ''
assert l.acquire('k','other',10) is False
assert l.renew('k','other',10) is False
assert l.release('k','other') is False
now[0] = 3
assert l.acquire('k','',2) is True
now[0] = 5
assert l.owner('k') is None
assert l.renew('k','',4) is False
assert l.release('k','') is False
assert l.acquire('k','b',3) is True
assert l.renew('k','b',5) is True
assert Leases(clock).owner('k') is None
assert l.release('k','b') is True
assert l.owner('k') is None
for call in [lambda:l.owner('missing'),lambda:l.release('missing','a'),lambda:l.renew('missing','a',2),lambda:l.acquire('new','a',1)]:
    n = calls[0]
    call()
    assert calls[0] == n+1
