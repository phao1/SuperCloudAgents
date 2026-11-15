from app import chunks
assert chunks([],2)==[]
assert chunks([1],3)==[[1]]
for n in [0,-1]:
 try: chunks([1],n)
 except ValueError: pass
 else: raise AssertionError(n)
