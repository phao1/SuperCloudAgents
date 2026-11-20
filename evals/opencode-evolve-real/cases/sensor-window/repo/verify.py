from dashboard import panel
from history import Sample

observed = [Sample("east", 1, 9), Sample("west", 1, 2)]
assert panel(observed, "east", 1, 4) == "east=27/3"
