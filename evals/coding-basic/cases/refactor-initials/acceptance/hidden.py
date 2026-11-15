from app import initials, initial
assert initials([" bob ","","  ","éva"])==["B","?","?","É"]
assert initial(" jane ")=="J"
assert initials([])==[]
