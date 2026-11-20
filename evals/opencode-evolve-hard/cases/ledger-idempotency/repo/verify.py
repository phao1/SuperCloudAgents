from app import apply
s={'balances':{},'seen':{}}
apply(s,[('x','a',2)])
assert s['balances']=={'a':2}
