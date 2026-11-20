import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '30f346ee7369cdcabf9a29969ca1cf81d87a5ae90f60a16f5727b039f43620f3'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import apply
state = {'a':4}
original = state
error = RuntimeError('stream')
def operations():
    yield 'b',2
    yield 'a',-1
    raise error
try:
    apply(state, operations())
except RuntimeError as caught:
    assert caught is error
else:
    raise AssertionError('stream error')
assert state is original and state == {'a':4}
assert apply(state,iter([('b',0),('a',-4),('a',1)])) is None
assert state == {'a':1,'b':0}
