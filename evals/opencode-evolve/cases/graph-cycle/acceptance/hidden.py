import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == '09aff4fdf68d06c25e31894a117d0d467f89225bdf7a0c5f339a6baa4973c978'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import order
graph = {'app':['lib','cfg'], 'lib':['cfg'], 'unused':[]}
assert order(graph) == ['cfg','lib','app','unused']
assert graph == {'app':['lib','cfg'], 'lib':['cfg'], 'unused':[]}
assert order({}) == []
for graph in [{'a':['a']}, {'ok':[], 'a':['b'], 'b':['a']}, {'a':['b'], 'b':['c'], 'c':['a']}]:
    try:
        order(graph)
    except ValueError:
        pass
    else:
        raise AssertionError('cycle accepted')
