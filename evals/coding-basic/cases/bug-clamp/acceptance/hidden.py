from app import clamp
assert [clamp(v,-2,2) for v in [-3,-2,0,2,3]]==[-2,-2,0,2,2]
