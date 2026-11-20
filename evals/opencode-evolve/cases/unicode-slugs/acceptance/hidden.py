import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '2d16815a73696235276fbb38e7c4cb5a07e99962cc960b4f86f0175d2e498f33'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import slug
assert slug('Straße CAFÉ') == 'strasse-café'
assert slug('Cafe\u0301') == 'café'
assert slug('中文__测试/２０２６') == '中文-测试-２０２６'
assert slug('\u0301A !\u0301 B') == 'a-b'
assert slug('किताब') == 'किताब'
assert slug('---') == ''
