from app import mean
assert mean([-2,0,2]) == 0
assert mean([1,2]) == 1.5
try: mean([])
except ValueError: pass
else: raise AssertionError("empty input")
