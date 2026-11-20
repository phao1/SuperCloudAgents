import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'd87768fab471f79ae1a45c97f967d1db1333cb0aaac1a9a5b3f7847952e10150'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import authenticate
assert authenticate('中', iter(['Owner','reader','READ','bad'])) == ['read','write']
assert authenticate(None,[]) == []
assert authenticate('u',[' guest ','READER']) == ['read']
import access.catalog as c
assert c.ROLES == {'admin': ('read','write'), 'reader': ('read',)}
assert c.ALIASES == {'owner':'admin','read':'reader','guest':'reader'}
c.ALIASES['custom'] = 'reader'
assert authenticate('u',['CUSTOM']) == ['read']
