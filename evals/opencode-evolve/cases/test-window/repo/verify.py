from app import windows
assert windows([1,2],2) == [(1,2)]
import subprocess, sys
subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],check=True)
