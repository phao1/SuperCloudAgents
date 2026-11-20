import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '51b41ac2eae11560b0075121bb04b99a0f8d6fea6e34d373c849074fb38f506f', 'archive/registry.py': '09fda8e19d503f463c09dcd1690b026b9eb1cebac9387e65762078a8549a5351'}
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
assert lookup('service-1561')['timeout'] == 0
content=Path('app.py').read_bytes()
lines=content.splitlines(keepends=True)
assert len(lines)==1806
assert hashlib.sha256(b''.join(lines[:1563])).hexdigest()=='e7aecd0128bf2e3fae0bee25fbfeae133fcfdb08a676e3283905ecfe903fe92e'
assert hashlib.sha256(b''.join(lines[1564:])).hexdigest()=='4410814efd00781ba7f8546e6c4eadb12a2887a075fe41935c6e25c2541e0ebf'
expected_line=b'    "service-1561": {"label": "\xe8\x8a\x82\xe7\x82\xb9-1561 e\xcc\x81", "retries": 3, "timeout": 0},  # shard=14\r\n'
assert lines[1563]==expected_line
record=lookup('service-1561')
record['label']='mutated'
assert lookup('service-1561')['label']!='mutated'
assert lookup('service-0000')=={'label':'节点-0000 e\u0301','retries':3,'timeout':30}
