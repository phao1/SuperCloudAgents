import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '04a2e61cd54db45477b1acc3ee3414cfe8fb93c1be3f774cf5122e1d2c2d9bf9'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
import app
assert set(app.CONVERTERS) == {'c','f','k'}
assert all(callable(f) for f in app.CONVERTERS.values())
assert abs(app.convert(273.15,'k')) < 1e-10
assert app.convert(-40,'f') == -40
app.CONVERTERS['c'] = lambda v: v + 9
assert app.convert(2,'c') == 11
try:
    app.convert(0, 'x')
except ValueError:
    pass
else:
    raise AssertionError('unit')
