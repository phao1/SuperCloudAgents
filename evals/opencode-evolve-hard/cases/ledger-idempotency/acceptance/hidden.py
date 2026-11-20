import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '37fff85415e6d5b111925f3509d22018b23c7b7c9b501496d9972b26296e566e'}
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

from app import apply
from copy import deepcopy
s = {'balances':{'a':2}, 'seen':{}}
b, seen = s['balances'], s['seen']
events = [('x','a',3),('x','a',3),('z','中',0)]
assert apply(s,iter(events)) is None
assert s == {'balances':{'a':5,'中':0},'seen':{'x':('a',3),'z':('中',0)}}
assert b is s['balances'] and seen is s['seen']
assert events == [('x','a',3),('x','a',3),('z','中',0)]
for event, message in [(('x','b',3),'conflict'), (('x','a',True),'invalid event'), (('bad','a',-99),'overdraft'), (('','a',1),'invalid event'), (('u','',1),'invalid event'), ((1,'a',1),'invalid event'), (('u',1,1),'invalid event'), (('u','a',1.0),'invalid event')]:
    before = deepcopy(s)
    rejects(lambda: apply(s,iter([('temp','a',2),event])),message)
    assert s == before and list(b) == list(before['balances']) and list(seen) == list(before['seen'])
error = RuntimeError('source')
def stream():
    yield ('new','a',8)
    raise error
before = deepcopy(s)
try:
    apply(s,stream())
except RuntimeError as caught:
    assert caught is error
else:
    raise AssertionError('iterator exception lost')
assert s == before
assert apply(s,[]) is None
