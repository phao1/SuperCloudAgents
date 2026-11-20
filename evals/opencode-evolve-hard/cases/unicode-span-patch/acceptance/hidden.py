import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': 'ba516cbf0d263e4a22d8f0bc6c65b6cbba15078ab882dc0fac0a2c133f106776'}
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

from app import patch
text='\ufeff中🙂e\u0301\r\n尾'
edits=[(7,8,'!'),(2,3,'星'),(2,2,'A'),(2,2,'B')]
assert patch(text,iter(edits))=='\ufeff中AB星e\u0301\r\n!'
assert edits==[(7,8,'!'),(2,3,'星'),(2,2,'A'),(2,2,'B')]
assert patch('abc',[(0,1,''),(1,2,'X'),(2,3,'\n')])=='X\n'
assert patch('abc',[(0,0,'x'),(3,3,'y')])=='xabcy'
assert patch(text,[])==text
assert patch('abc',[(0,2,'x'),(2,2,'y')])=='xyc'
for edits in [[(-1,0,'')],[(2,1,'')],[(0,4,'')],[(True,1,'')],[(0,1.0,'')],[(0,1,None)]]:
    rejects(lambda:patch('abc',edits),'invalid edit')
for edits in [[(0,2,'x'),(1,1,'y')],[(0,2,'x'),(1,3,'y')],[(0,1,'x'),(0,1,'y')]]:
    rejects(lambda:patch('abc',edits),'overlap')
