from app import slug
assert slug("  A__B!!! C  ")=="a-b-c"
assert slug("!!!")==""
assert slug("X9")=="x9"
