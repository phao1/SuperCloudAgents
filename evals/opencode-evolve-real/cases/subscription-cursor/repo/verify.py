from journal import Index, Record
from subscription import collect

index = Index([Record(2, "index", "hidden"), Record(3, "entry", "visible")])
assert collect(index, 0, 8, 1) == (["visible"], 8)
