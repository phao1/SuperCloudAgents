import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'ad41270a79319ee6bd81ee7b51ae354f2a19337ec19ae25d74d6cf922a6aa9bc'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import cents
from decimal import getcontext
before = getcontext().copy()
getcontext().prec = 6
for text, expected in [('0',0),('-1.005',-101),('1.004',100),('2e-2',2),('10000000000000000000000000000000000000000.01',10**42+1)]:
    assert cents(text) == expected
assert getcontext().prec == 6
getcontext().prec = before.prec
for text in ['NaN','Infinity','-Infinity','bad','']:
    try:
        cents(text)
    except ValueError:
        pass
    else:
        raise AssertionError(text)
assert getcontext().flags == before.flags and getcontext().traps == before.traps
