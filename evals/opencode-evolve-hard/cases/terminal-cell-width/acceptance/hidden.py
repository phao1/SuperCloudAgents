import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '73afcef090bb805036d88c5e7c28331b1bf0883290369e297c310ca4440753e5'}
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

from app import width
for text, result in [('',0),('abc',3),('中🙂',4),('e\u0301',1),('\u0301',0),('a\t中',6),('abcd\t',8),('longer\nx',6),('x\n中🙂\n',4),('ab\rc',3),('👩\u200d💻',4),('\u200bX\ufe0f',1),('Ａ·',3),('\u0903',1)]:
    assert width(text)==result,(text,width(text))
for char in ['\0','\x1b','\x7f','\x85']:
    rejects(lambda:width('x'+char),f'control U+{ord(char):04X}')
