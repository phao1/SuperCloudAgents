import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '580486977198e8837ae17740122d6eb507e1e8f6ca2f014591f759f081c51e92'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import route
events = [{'type':'DELETED','id':'中'}, {'type':' created ','id':'x'}, {'type':'CREATED','id':'中'}, {'type':'deleted','id':'x'}, {'type':'unknown'}]
from copy import deepcopy
before = deepcopy(events)
result = route(iter(events))
assert result == {'中':1,'x':0} and list(result) == ['中','x'] and events == before
assert route([]) == {}
for event in [{'type':'created'}, {'type':'deleted','id':1}]:
    try:
        route([event])
    except ValueError:
        pass
    else:
        raise AssertionError('ID')
