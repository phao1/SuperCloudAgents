from app import apply
state = {'a':2}
try:
    apply(state, [('a',-1),('b',-2)])
except ValueError:
    pass
else:
    raise AssertionError('negative')
assert state == {'a':2}
assert apply(state,[('a',3)]) is None and state == {'a':5}
