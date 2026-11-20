import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': 'a1ad8590b20c124ba4816c86fa6bd09811f93c8d91e443955f76c2409c5e43fe'}
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

from app import reduce
assert reduce([]) == {}
events=[('中','start'),('b','cancel'),('中','start'),('中','fail'),('中','fail'),('中','retry'),('中','retry'),('中','start'),('中','succeed'),('中','succeed'),('b','cancel')]
assert reduce(iter(events)) == {'中':('done',2),'b':('cancelled',0)}
assert list(reduce(events)) == ['中','b']
assert reduce([('a','start'),('a','fail'),('a','cancel')]) == {'a':('cancelled',1)}
for prefix, action in [([], 'retry'),([], 'succeed'),([], 'fail'),([], 'unknown'),([('a','start')],'retry'),([('a','cancel')],'start'),([('a','start'),('a','succeed')],'start'),([('a','start'),('a','fail')],'succeed')]:
    rejects(lambda: reduce(prefix+[('a',action)]),f'a:{action}')
assert events[0] == ('中','start')
