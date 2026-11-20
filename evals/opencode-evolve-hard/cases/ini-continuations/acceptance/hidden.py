import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '38546e03e7a20177093381bdab692781573fa93f3610345102e1ce4783a8d8ae'}
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
assert parse('') == {}
assert parse(' # hi\r\n;there\n\nx= a=b # literal\n next = line\n\n ;skip\n last\n[ 中 ]\r\nK=🙂\n[中]\nK=z\nJ=2') == {'':{'x':'a=b # literal\nnext = line\nlast'},'中':{'K':'z','J':'2'}}
assert list(parse('[s]\nb=1\na=2\nb=3')['s']) == ['b','a']
assert parse('x=1\n [fake]\n [also]') == {'':{'x':'1\n[fake]\n[also]'}}
for text, line in [(' x=1',1), ('[s]\n c',2), ('[ ]',1), ('[a',1), ('[a]]',1), ('[[a]',1), ('\n\nx',3), ('=v',1)]:
    rejects(lambda: parse(text), f'line {line}')
assert parse('[empty]\n# a') == {'empty':{}}
