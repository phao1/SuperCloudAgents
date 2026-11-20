import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '72e270e5ee92519be17cbca1de65d823356f1a11d5055d79ac7ef71530d93779'}
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

import app,web,json,unittest
rows=[('中;🙂',0),('a"b',2),('x\r\ny',3),('',4)]
expected='name;count\r\n"中;🙂";0\r\n"a""b";2\r\n"x\r\ny";3\r\n;4\r\n'
assert app.render(iter(rows))==expected and rows[0]==('中;🙂',0)
assert app.render([])=='name;count\r\n'
assert web.render(rows)==b'\xef\xbb\xbf'+expected.encode()
for bad in [True,False,-1,1.0,'2',None]:
    rejects(lambda:app.render([('ok',bad)]),'invalid count')
for bad in [None,1,False]: rejects(lambda:app.render([(bad,1)]),'invalid name')
assert json.loads(Path('dialect.json').read_text())=={'delimiter':';','lineterminator':'\r\n'}
original=Path('dialect.json').read_bytes()
try:
    Path('dialect.json').write_text('{"delimiter":"|","lineterminator":"\\r\\n"}')
    assert app.render([('a|b',1)])=='name|count\r\n"a|b"|1\r\n'
finally: Path('dialect.json').write_bytes(original)
original_render=app.render
try:
    app.render=lambda rows:'probe'
    assert web.render([])==b'\xef\xbb\xbfprobe'
finally: app.render=original_render
for data,code,out,err in [(json.dumps(rows),0,expected.encode(),b''),('not-json',2,b'',b'invalid input\n'),('[ ["x", true] ]',2,b'',b'invalid input\n')]:
    p=subprocess.run([sys.executable,'-B','-m','cli'],input=data.encode(),capture_output=True,timeout=3)
    assert (p.returncode,p.stdout,p.stderr)==(code,out,err)
assert Path('GUIDE.md').read_bytes()==b'CSV: semicolon; CRLF; web=UTF-8-BOM; cli=UTF-8.\n'
suite=unittest.defaultTestLoader.discover('tests')
assert suite.countTestCases()>=3 and unittest.TextTestRunner().run(suite).wasSuccessful()
