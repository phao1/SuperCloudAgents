from app import Cache
now = [0]
c = Cache(lambda: now[0])
c.put('x', 3, 2)
now[0] = 3
assert c.get('x') is None
