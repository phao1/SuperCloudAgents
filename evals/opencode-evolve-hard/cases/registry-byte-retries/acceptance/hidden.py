import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': 'b48ec65f5b315423e5f549125fe6250ea921c314cde3aa9745ffccce76fecec9', 'archive/registry.py': '09fda8e19d503f463c09dcd1690b026b9eb1cebac9387e65762078a8549a5351'}
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
assert lookup('service-1377')['retries'] == 0
content=Path('app.py').read_bytes()
lines=content.splitlines(keepends=True)
assert len(lines)==1806
assert hashlib.sha256(b''.join(lines[:1379])).hexdigest()=='54ed0c240c03bc61b00bc0d1536d97adc6e5e9fff021f5ef96f364697d49809b'
assert hashlib.sha256(b''.join(lines[1380:])).hexdigest()=='825859fe3a6307fcd7617756559e61b1f08c71e755de5d1f3e32ae236f5485d4'
expected_line=b'    "service-1377": {"label": "\xe8\x8a\x82\xe7\x82\xb9-1377 e\xcc\x81", "retries": 0, "timeout": 30},  # shard=00\r\n'
assert lines[1379]==expected_line
record=lookup('service-1377')
record['label']='mutated'
assert lookup('service-1377')['label']!='mutated'
assert lookup('service-0000')=={'label':'节点-0000 e\u0301','retries':3,'timeout':30}
