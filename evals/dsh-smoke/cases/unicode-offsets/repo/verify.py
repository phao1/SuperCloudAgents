from app import replace_spans
assert replace_spans('abcdef',[(1,3,'X'),(4,6,'Y')]) == 'aXdY'
