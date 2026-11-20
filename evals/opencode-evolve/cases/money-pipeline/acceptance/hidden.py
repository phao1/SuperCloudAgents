import hashlib
import subprocess
import sys
from pathlib import Path

assert hashlib.sha256(Path('verify.py').read_bytes()).hexdigest() == 'f4585b76117159a288f5630960357da1833eed4b83c2353b6c647bbf7b85dea4'
subprocess.run([sys.executable, 'verify.py'], check=True, timeout=15)
from app import summarize
import csv, io
rows = [{'currency':'中,"\n文','quantity':2,'cents':7}, {'currency':'EUR','quantity':0,'cents':9}, {'currency':'中,"\n文','quantity':1,'cents':2}]
from copy import deepcopy
before = deepcopy(rows)
assert list(csv.reader(io.StringIO(summarize(iter(rows))))) == [['currency','cents'], ['中,"\n文','16'], ['EUR','0']]
assert rows == before
assert summarize([]) == 'currency,cents\n'
for key, value in [('quantity',True), ('quantity',-1), ('cents',False), ('cents',1.5), ('currency','')]:
    row = {'currency':'USD','quantity':1,'cents':3}
    row[key] = value
    try:
        summarize([row])
    except ValueError:
        pass
    else:
        raise AssertionError((key,value))
