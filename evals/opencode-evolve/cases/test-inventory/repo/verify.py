from app import reserve
assert reserve({'a':3},{'a':1}) == {'a':2}
import subprocess, sys
subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],check=True)
