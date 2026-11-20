import subprocess, sys
result = subprocess.run([sys.executable,'-m','cli','1','4'], capture_output=True, text=True)
assert result.returncode == 0 and result.stdout == '1,2,3\n'
