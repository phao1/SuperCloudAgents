from app import Cache

now = 0
events = []
cache = Cache(2, lambda: now, lambda *event: events.append(event))
cache.put("a", 1, 2)
cache.put("b", 2, 2)
assert cache.get("a") == 1
now = 2
cache.put("a", 3)
assert cache.items() == [("a", 3)]
assert events == [("b", 2, "expired"), ("a", 1, "expired")]
