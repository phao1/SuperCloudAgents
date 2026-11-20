from app import patch
assert patch({'x':1}, {'x':None,'y':2}) == {'y':2}
