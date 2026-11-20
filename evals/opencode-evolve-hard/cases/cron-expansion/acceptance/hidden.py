import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '656058b62afeab1bf897cd640536f417eaa8281c4e0e587e0107a499a9374a4e'}
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

from app import expand
for field, lo, hi, result in [('*',1,5,[1,2,3,4,5]), ('*/2',1,6,[1,3,5]), ('3/2',0,8,[3,5,7]), ('02-06/02,3,2',0,8,[2,3,4,6]), ('0',0,0,[0]), ('4,2,4',0,9,[2,4])]:
    assert expand(field,lo,hi) == result
for field in ['', '1,', ',1', '1,,2', ' 1', '1 ', '+1','١','1/0','*/00','4-2','0-9','1//2','1-2-3','*/2/3','*,bogus']:
    rejects(lambda: expand(field,1,8), 'invalid field')
