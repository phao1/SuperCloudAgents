from app import parse_pair
assert parse_pair(" a = b=c ")==("a","b=c")
assert parse_pair("x=")==("x","")
for value in ["abc"," =x"]:
 try: parse_pair(value)
 except ValueError: pass
 else: raise AssertionError(value)
