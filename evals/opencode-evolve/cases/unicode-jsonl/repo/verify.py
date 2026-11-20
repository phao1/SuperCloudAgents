from app import encode,decode
assert encode([{'中':1}]) == '{"中":1}\n'
assert decode('1\n2\n') == [1,2]
