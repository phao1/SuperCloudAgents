import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '946182966f921265fb82e3685133ad6f6881bdc48384db65ef00508e6a082e1c', 'tests/test_contract.py': '2c9a577dbd04f4c28eec894c96d50a282a26c51e09c6748bbcd450348531791b'}
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

subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests'],check=True,timeout=8)
from app import apply
assert apply('a\nb\nc\n','@@ -1,1 +0,0 @@\n-a\n@@ -3,1 +2,1 @@\n-c\n+C\n')=='b\nC\n'
