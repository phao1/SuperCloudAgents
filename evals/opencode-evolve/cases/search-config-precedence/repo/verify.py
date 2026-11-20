from app import resolve
assert resolve({'x':1},{'x':2},{'x':0}) == {'x':0}
