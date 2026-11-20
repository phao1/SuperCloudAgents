import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'cec3528fd5b9363a7813efb6b0265057f78935818fa0c7adc052a9bf8f1861b8'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import resolve
defaults, file_values, environment = {'a':1,'b':2}, {'b':False,'c':9}, {'a':None,'c':'','d':0}
assert resolve(defaults,file_values,environment) == {'a':None,'b':False,'c':'','d':0}
assert defaults == {'a':1,'b':2} and file_values == {'b':False,'c':9}
out = resolve(defaults,{}, {})
out['a'] = 9
assert defaults['a'] == 1
assert resolve({}, {}, {}) == {}
