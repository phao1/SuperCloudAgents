import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': 'b286ac64b4e82c0fb4207e27bec66826ab087e8a3066697b8fd47d885143e247', 'archive/registry.py': '09fda8e19d503f463c09dcd1690b026b9eb1cebac9387e65762078a8549a5351'}
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

from app import lookup
assert lookup('service-0803')['label'] == '中文「归档」🙂'
content=Path('app.py').read_bytes()
lines=content.splitlines(keepends=True)
assert len(lines)==1806
assert hashlib.sha256(b''.join(lines[:805])).hexdigest()=='ac04f4c2423e946bde694f1c519db3365c7059b94280da4b086cf14a535eb9b3'
assert hashlib.sha256(b''.join(lines[806:])).hexdigest()=='2f91ab61775f598772e0ed9f8dd73a7f178efa004b360821b03038c29568387c'
expected_line=b'    "service-0803": {"label": "\xe4\xb8\xad\xe6\x96\x87\xe3\x80\x8c\xe5\xbd\x92\xe6\xa1\xa3\xe3\x80\x8d\xf0\x9f\x99\x82", "retries": 3, "timeout": 30},  # shard=04\r\n'
assert lines[805]==expected_line
record=lookup('service-0803')
record['label']='mutated'
assert lookup('service-0803')['label']!='mutated'
assert lookup('service-0000')=={'label':'节点-0000 e\u0301','retries':3,'timeout':30}
