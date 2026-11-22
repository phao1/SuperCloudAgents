from app import pairs
assert list(pairs([1,2],['a','b'])) == [(1,'a'),(2,'b')]
for left,right in [([1],[]),([],[1]),([1,2],[3])]:
    try:
        list(pairs(left,right))
    except ValueError:
        pass
    else:
        raise AssertionError('mismatch')
