from app import order
assert order({'a':['b'], 'b':[]}) == ['b','a']
