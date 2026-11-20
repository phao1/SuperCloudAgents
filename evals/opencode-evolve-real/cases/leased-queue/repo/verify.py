from app import Queue

now = 4
queue = Queue(lambda: now, 2)
assert queue.submit("x", "payload")
assert queue.reserve(1, 2) == [("x", "payload", 1, 6)]
now = 9
assert queue.snapshot() == {"x": ("pending", 1, 6)}
assert queue.reserve(1, 2) == [("x", "payload", 2, 11)]
assert not queue.ack("x", 1)
