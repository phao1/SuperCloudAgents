import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'ef3528bdeff572c6d9df145560841fe16bcc8d9065ff0d53aa0d82a3161ce960'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import compare_versions as compare
for a,b,value in [('1.2','1.2.0',0),('01.002','1.2',0),('0','0.0',0),('1','1.0.1',-1),('999999999999999999999','2',1)]:
    assert compare(a,b) == value and compare(b,a) == -value
for invalid in ['', '1.', '.1','1..2','-1','+1',' 1','１','1.a']:
    for args in [(invalid,'1'),('1',invalid)]:
        try:
            compare(*args)
        except ValueError:
            pass
        else:
            raise AssertionError(args)
