from app import parse
assert parse('a = b\na=c') == {'a':'c'}
