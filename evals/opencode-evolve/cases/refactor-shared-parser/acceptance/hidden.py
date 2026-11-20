import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '6f29876e37f26a447f4de051cdca2d38934b0e8638f799033c6df60c83b3e806'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
import app
assert app.parse_numbers(' , -2, +3, ,0') == [-2,3,0]
assert app.total('') == 0 and app.largest(',,') is None
assert app.largest('-8,-3') == -3
for function in [app.total, app.largest]:
    try:
        function('1,bad')
    except ValueError:
        pass
    else:
        raise AssertionError('invalid field')
app.parse_numbers = lambda text: [7,9]
assert app.total('ignored') == 16 and app.largest('ignored') == 9
