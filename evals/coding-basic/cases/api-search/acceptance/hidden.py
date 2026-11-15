from app import find
assert find([2,1,2],2)==[0,2]
assert find([],2)==[]
assert find([[1],[2],[1]],[1])==[0,2]
