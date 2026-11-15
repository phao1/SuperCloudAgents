from app import merge_counts
a={"x":2}; b={"y":-1,"x":-2}
assert merge_counts(a,b)=={"x":0,"y":-1}
assert a=={"x":2} and b=={"y":-1,"x":-2}
assert merge_counts()=={}
