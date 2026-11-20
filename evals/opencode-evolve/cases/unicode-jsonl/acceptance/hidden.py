import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '09dc45778e71873405280306fc285a55cb6a8cd10f53f3da8cf34a361879dee8'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import encode,decode
records = [{'标题':'中文\n🙂e\u0301','rows':[None,False,0,'', '\u2028\u0085']}, [], '終']
from copy import deepcopy
before = deepcopy(records)
text = encode(iter(records))
assert text.count('\n') == 3 and '中文' in text and '\\u4e2d' not in text
assert decode(text) == records == before
assert encode([]) == '' and decode(' \r\n\t\n') == []
assert decode('1\r\n \r\nfalse\r\n') == [1,False]
try:
    decode('1\n{broken}\n')
except ValueError:
    pass
else:
    raise AssertionError('malformed')
