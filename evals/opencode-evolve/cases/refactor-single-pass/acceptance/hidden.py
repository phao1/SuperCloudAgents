import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '44a23798f82ba02283ccbe3a336cd21705f9182d7991e931983df36413fd3061'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
import app
assert app.stats(iter([])) == (0,0,None,None)
assert app.stats(iter([-8,-2,-5])) == (3,-15,-8,-2)
assert app.stats(iter([10**80,1])) == (2,10**80+1,1,10**80)
import tracemalloc
tracemalloc.start()
assert app.stats(iter(range(100000))) == (100000,4999950000,0,99999)
assert tracemalloc.get_traced_memory()[1] < 1000000
tracemalloc.stop()
