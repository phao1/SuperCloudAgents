import hashlib
import subprocess
import sys
from pathlib import Path

PROTECTED = {'verify.py': '2263b082d76d88f7636481a84668e2a95d50d35b2199e316274cee2aab6711ab'}
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

from app import index
words=[' ',' É ','e\u0301',' É ','STRASSE','Straße','中🙂','中🙂','\u0130','i\u0307','\u0390']
out=index(iter(words))
assert list(out)==['é','strasse','中🙂','i\u0307','ΐ']
assert out['é']==[(1,' É '),(2,'e\u0301')]
assert out['strasse']==[(4,'STRASSE'),(5,'Straße')]
assert out['中🙂']==[(6,'中🙂')]
assert out['i\u0307']==[(8,'\u0130'),(9,'i\u0307')]
assert words[0]==' ' and words[2]=='e\u0301'
assert index([])=={} and index(['','\t\n'])=={}
for bad in [None,False,1,[],{}]:
    rejects(lambda:index(['ok',' ',bad]),'word 2')
out['é'].clear()
assert len(index(words)['é'])==2
