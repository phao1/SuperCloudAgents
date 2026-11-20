import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': 'c6dff1bcda5805edfe2ee1908a9f60cdd21dc0e68bb9bb5217ba905971b32b3b', 'tests/test_contract.py': '5777e244765ed1d0ddc8d2e3033c2ccb3710e140715240d90af6fa6039785fa3'}
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
from app import order
values=['1.0.0','1.0.0-rc.1','1.0.0-beta.11','1.0.0-beta.2','1.0.0-beta','1.0.0-alpha.beta','1.0.0-alpha.1','1.0.0-alpha']
assert order(iter(values))==values[::-1]
assert order(['999999999999999999.0.0','2.0.0'])==['2.0.0','999999999999999999.0.0']
