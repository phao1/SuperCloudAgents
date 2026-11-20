import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': 'db6263b37ffe76e4c15430123b0ad77fac5862aa274d97933c096e4a3f0b5e6f'}
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

import app,api,audit,json,unittest
from copy import deepcopy
value={' Token ':None,'nested':[{'PASSWORD':{'x':1},'api_key':False,'ok':['中🙂']}],'z':0}
before=deepcopy(value)
expected={' Token ':'[redacted]','nested':[{'PASSWORD':'[redacted]','api_key':'[redacted]','ok':['中🙂']}],'z':0}
out=app.redact(value)
assert out==expected and value==before and list(out)==list(value)
out['nested'][0]['ok'].append('new')
assert value==before
assert api.render(value)==json.dumps(expected,ensure_ascii=False,separators=(',',':'))
assert audit.render(value)==json.dumps(expected,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n'
assert json.loads(Path('policy.json').read_text())=={'fields':['password','token','api_key']}
for scalar in [None,False,0,'中']:
    assert app.redact(scalar)==scalar
original=Path('policy.json').read_bytes()
try:
    Path('policy.json').write_text('{"fields":["other"]}')
    assert app.redact({'other':1,'token':2})=={'other':'[redacted]','token':2}
finally: Path('policy.json').write_bytes(original)
original_redact=app.redact
try:
    app.redact=lambda value:{'patched':'中'}
    assert api.render({})=='{"patched":"中"}'
    assert audit.render({})=='{"patched":"中"}\n'
finally: app.redact=original_redact
assert Path('GUIDE.md').read_bytes()==b'Sensitive fields: password, token, api_key. Marker: [redacted].\n'
suite=unittest.defaultTestLoader.discover('tests')
assert suite.countTestCases()>=3 and unittest.TextTestRunner().run(suite).wasSuccessful()
