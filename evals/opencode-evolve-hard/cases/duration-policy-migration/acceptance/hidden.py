import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '652909092d28255ca47d9c3c534d7427b1c84eda7248ebde5bff9a362dd4eaa5'}
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

from app import parse
import service,json,unittest
for value, result in [('0ms',0),(' 002s\t',2000),('2m',120000),('120000ms',120000),('250ms',250)]:
    assert parse(value)==result
for value in ['','1','1S','-1s','1.5s','١s','1 s','+1s']:
    rejects(lambda:parse(value),'invalid duration')
rejects(lambda:parse('120001ms'),'duration too large')
assert service.timeout()==250
rejects(lambda:service.timeout(''),'invalid duration')
assert json.loads(Path('policy.json').read_text())=={'default':'250ms','max_ms':120000}
original=Path('policy.json').read_bytes()
try:
    Path('policy.json').write_text('{"default":"3s","max_ms":4000}')
    assert service.timeout()==3000
    rejects(lambda:parse('5s'),'duration too large')
finally:
    Path('policy.json').write_bytes(original)
import app
original_parse=app.parse
try:
    app.parse=lambda text:777
    assert service.timeout('1s')==777
finally: app.parse=original_parse
for args, code, stdout, stderr in [([],0,'250\n',''),(['2s'],0,'2000\n',''),(['bad'],2,'','invalid duration\n'),(['1s','2s'],2,'','invalid duration\n')]:
    p=subprocess.run([sys.executable,'-B','-m','cli',*args],capture_output=True,text=True,timeout=3)
    assert (p.returncode,p.stdout,p.stderr)==(code,stdout,stderr)
assert Path('GUIDE.md').read_bytes()==b'default=250ms; max=120000ms; units=ms,s,m\n'
suite=unittest.defaultTestLoader.discover('tests')
assert suite.countTestCases()>=3
result=unittest.TextTestRunner().run(suite)
assert result.wasSuccessful()
