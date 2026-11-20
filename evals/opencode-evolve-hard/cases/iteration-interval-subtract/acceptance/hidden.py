import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '668ba8bb61e391252856fe81ecaa45e1271a14a1a6e3b9c4743c9fd3d2f86f63', 'tests/test_contract.py': 'e8bebb8f2cd4cfe60fb38c0301907a1707452ab1cdab91a6067148f2b089f863'}
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
from app import subtract
for start in range(-3,4):
    for stop in range(start,5):
        out=subtract([(start,stop)],[(0,2)])
        expected=set(range(start,stop))-set(range(0,2))
        assert {n for a,b in out for n in range(a,b)}==expected
