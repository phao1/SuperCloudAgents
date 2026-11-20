from app import row
assert row(['a','b']) == 'a,b\r\n'
import subprocess, sys
subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],check=True)
