"""Hidden acceptance for this standalone fixture."""

import signal
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

checks = 0


def check(actual, expected):
    global checks
    expected_type = type(expected)
    assert isinstance(actual, expected_type) and actual == expected, (actual, expected)
    if expected_type in (bool, int):
        assert type(actual) is expected_type, (actual, expected)
    checks += 1


def raises(error_type, call, *args, **kwargs):
    global checks
    try:
        call(*args, **kwargs)
    except error_type as error:
        checks += 1
        return error
    raise AssertionError(f"{call!r} did not raise {error_type.__name__}")


def verify():
    for name, digest in PROTECTED.items():
        assert sha256(Path(name).read_bytes()).hexdigest() == digest, name
    result = subprocess.run([sys.executable, "-B", "verify.py"], check=False, timeout=12)
    if result.returncode != 0:
        print("Public verification failed.", file=sys.stderr)
        raise SystemExit(1)

PROTECTED = {
    'SPEC.md': '5ca4ebbd8554d9b8d90ad6875569794e0e15ac6911181441fa456c776a60a6a9',
    'verify.py': 'f134c995b33aea40700a1b0ae985b3df0af664e1b4bb39ec021f8ab801142925',
}


def check_feature():
    from app import Queue

    class Clock:
        def __init__(self, tick):
            self.tick = tick
            self.reads = []

        def __call__(self):
            self.reads.append(self.tick)
            return self.tick

    for base in (0, 1, 2**63 + 7):
        for maximum in (1, 2, 4):
            clock = Clock(base)
            queue = Queue(clock, maximum)
            check(queue.submit("a", "", priority=0), True)
            check(queue.submit("b", "雪", priority=1, delay=2), True)
            check(queue.submit("c", "none", priority=0), True)
            check(queue.submit("d", "same", priority=0, delay=1), True)
            check(queue.submit("a", ""), False)
            assert len(clock.reads) == 4
            clock.tick += 1
            check(queue.reserve(2, 2), [("a", "", 1, base + 3), ("c", "none", 1, base + 3)])
            check(queue.ack("unknown", 1), False)
            check(queue.ack("c", 1), True)
            check(queue.ack("c", 1), False)
            clock.tick += 2
            check(queue.retry("a", 1), False)
            snapshot = queue.snapshot()
            expected_a = ("dead", 1, None) if maximum == 1 else ("pending", 1, base + 3)
            check(snapshot, {"a": expected_a, "b": ("pending", 0, base + 2),
                             "c": ("done", 1, None), "d": ("pending", 0, base + 1)})
            check(list(snapshot), ["a", "b", "c", "d"])
            snapshot.clear()
            check(queue.reserve(2, 1), [("b", "雪", 1, base + 4), ("d", "same", 1, base + 4)])
            check(queue.retry("b", 1, delay=10), True)
            if maximum == 1:
                check(queue.reserve(3, 1), [])
            else:
                check(queue.reserve(3, 1), [("a", "", 2, base + 4)])
                check(queue.ack("a", 1), False)
            clock.tick += 1
            state = queue.snapshot()
            assert state["d"] == (("dead", 1, None) if maximum == 1 else ("pending", 1, base + 4))
            assert state["b"] == (("dead", 1, None) if maximum == 1 else ("pending", 1, base + 13))
            if maximum == 4:
                check(state["a"], ("pending", 2, base + 4))
                check(queue.reserve(3, 1), [("a", "", 3, base + 5), ("d", "same", 2, base + 5)])
                check(queue.retry("a", 3), True)
                check(queue.reserve(4, 5), [("a", "", 4, base + 9)])
                check(queue.retry("a", 4, delay=900), True)
                check(queue.snapshot()["a"], ("dead", 4, None))
            assert str(raises(ValueError, queue.submit, "c", "changed")) == "conflict"
            check(queue.submit("c", "none"), False)
            assert clock.reads == sorted(clock.reads)

    now = 10
    queue = Queue(lambda: now, 1)
    queue.submit("x", "", priority=-100)
    for method, args, kwargs in [
        (queue.submit, ("x", 1), {}), (queue.submit, ("x", ""), {"delay": -1}),
        (queue.submit, ("x", ""), {"priority": True}), (queue.reserve, (0, 4), {}),
        (queue.reserve, (1, 0), {}), (queue.ack, ("", 1), {}), (queue.ack, ("x", True), {}),
        (queue.retry, ("x", 1), {"delay": False}),
    ]:
        now += 1
        assert str(raises(ValueError, method, *args, **kwargs)) == "arguments"
    now = 10
    check(queue.reserve(1, 1), [("x", "", 1, 11)])
    for bad in (9, -1, False):
        now = bad
        assert str(raises(ValueError, queue.snapshot)) == "clock"
    now = 11
    check(queue.ack("x", 1), False)
    check(queue.snapshot(), {"x": ("dead", 1, None)})
    check(Queue(lambda: 0, 2).snapshot(), {})
    assert str(raises(ValueError, Queue, lambda: 0, 0)) == "configuration"
    error = LookupError("clock")
    queue = Queue(lambda: (_ for _ in ()).throw(error), 1)
    assert raises(LookupError, queue.snapshot) is error


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
