import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '00e79df5a6e4247c4d166e032fa75a2f33fb2afa73934d8ab4626736e23c605f'}
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
for text, expected in [('',[]), ('\n',[['']]), ('\r\n\r\n',[[''],['']]), (',',[['','']]), ('a,\n',[['a','']]), ('""',[['']]), ('"a,b","中""🙂"\r\n',[['a,b','中"🙂']]), ('"a\r\nb\nc",x',[['a\r\nb\nc','x']]), (' a , b ',[[' a ',' b ']]), ('\ufeffx,y',[['\ufeffx','y']])]:
    assert parse(text) == expected, text
for text in ['a\rb','a"b',' "x"','"x" ','"x"y','"x','"""','a\r']:
    rejects(lambda: parse(text), 'invalid csv')
