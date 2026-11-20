import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '563e5e6379d380e471f033ebd3ab61827bcb176fad4364a22db59632a7ffab19'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import parse
assert parse(' # skip\n\na\\=b = 中\\#文=好\nempty=\nspace=\\ x\\ ') == {'a=b':'中#文=好', 'empty':'', 'space':' x '}
assert parse('a=one#two\na=last') == {'a':'last'}
assert parse('path=a\\\\b') == {'path':'a\\b'}
for text in ['no separator', '=x', 'a=bad\\q', 'a=bad\\']:
    try:
        parse(text)
    except ValueError:
        pass
    else:
        raise AssertionError(text)
