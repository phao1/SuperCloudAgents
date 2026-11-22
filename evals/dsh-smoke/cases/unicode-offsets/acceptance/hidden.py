import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '8c39a58eff0a8fcb3d7bc9ceb9e7265eae658fee861ddfeed8a7a73c19243be4'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import replace_spans as replace
text = '中文e\u0301\r\n🙂end'
assert replace(text,iter([(6,7,'猫'),(0,1,'汉')])) == '汉文e\u0301\r\n猫end'
assert replace('abc', [(1,2,'B'),(1,1,'x'),(1,1,'y')]) == 'axyBc'
assert replace('abc', [(0,1,'A'),(1,2,'B')]) == 'ABc'
assert replace(text, []) == text
for spans in [[(-1,0,'')],[(1,0,'')],[(0,99,'')],[(0,2,''),(1,1,'')],[(0,2,''),(1,3,'')]]:
    try:
        replace('abc',spans)
    except ValueError:
        pass
    else:
        raise AssertionError(spans)
