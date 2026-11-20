from app import parse
assert parse('[s]\nx=1') == {'s':{'x':'1'}}
