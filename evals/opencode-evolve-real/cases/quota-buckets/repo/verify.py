from app import Buckets, Decision

ticks = iter([10, 11, 12])
buckets = Buckets(3, 2, 3, lambda: next(ticks))
assert buckets.attempt({"read": 3}) == Decision(True, None, {"read": 0})
assert buckets.attempt({"read": 1, "write": 1}) == Decision(False, 12, {"read": 0, "write": 3})
assert buckets.attempt({"read": 1, "write": 1}) == Decision(True, None, {"read": 0, "write": 2})
