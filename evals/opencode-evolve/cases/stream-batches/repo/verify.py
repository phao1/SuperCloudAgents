from app import batches
assert list(batches(range(5),2)) == [(0,1),(2,3),(4,)]
