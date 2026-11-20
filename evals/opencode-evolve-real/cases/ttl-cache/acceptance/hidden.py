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
    'SPEC.md': '28c832bf26bcd0bb2f082b0f6854c5092f815ac7dbe869bfa296b005fa0de482',
    'verify.py': '840b18ec7a0fad5799bbe507a0916347783685e16dbaab834abe1388511f62c0',
}


def check_feature():
    from random import Random

    from app import Cache

    class Clock:
        def __init__(self):
            self.tick = 0
            self.reads = []

        def __call__(self):
            self.reads.append(self.tick)
            return self.tick

    for capacity in (1, 2, 4, 8):
        clock = Clock()
        events = []
        def record(*event, events=events):
            events.append(event)

        cache = Cache(capacity, clock, record)
        model = []
        rng = Random(120 + capacity)
        for step in range(30):
            key = rng.choice(["a", "b", "é", "e\u0301", " "])
            value = None if step % 3 == 0 else [step]
            ttl = rng.choice([None, 1, 2, 5])
            clock.tick += rng.choice([0, 0, 0, 1, 3])
            now = clock.tick
            expired = sorted(
                (deadline, position) for position, (_, _, deadline) in enumerate(model)
                if deadline is not None and deadline <= now
            )
            expected = [(model[position][0], model[position][1], "expired") for _, position in expired]
            model = [entry for position, entry in enumerate(model) if position not in {i for _, i in expired}]
            existing = next((entry for entry in model if entry[0] == key), None)
            operation = rng.choice(["get", "put", "put", "delete", "items"])
            if operation == "get":
                missing = object()
                output = cache.get(key, missing)
                if existing is not None:
                    model.remove(existing)
                    model.append(existing)
                assert output is (existing[1] if existing is not None else missing)
            elif operation == "put":
                check(cache.put(key, value, ttl), None)
                if existing is not None:
                    model.remove(existing)
                    expected.append((key, existing[1], "replaced"))
                model.append((key, value, now + ttl if ttl is not None else None))
                if len(model) > capacity:
                    old_key, old_value, _ = model.pop(0)
                    expected.append((old_key, old_value, "capacity"))
            elif operation == "delete":
                assert cache.delete(key) is (existing is not None)
                if existing is not None:
                    model.remove(existing)
                    expected.append((key, existing[1], "deleted"))
            else:
                output = cache.items()
                assert output == [(key, value) for key, value, _ in model]
                output.clear()
            check(events, expected)
            events.clear()
            assert clock.reads == sorted(clock.reads) and len(clock.reads) == step + 1

    now = 100
    failures = []
    first, second = LookupError("first"), RuntimeError("second")
    def callback(key, value, reason):
        failures.append((key, value, reason))
        if key == "first":
            raise first
        if key == "second":
            raise second

    cache = Cache(8, lambda: now, callback)
    value = object()
    cache.put("later", 3, 3)
    cache.put("first", 1, 1)
    cache.put("second", value, 2)
    cache.put("stays", 9, None)
    now += 3
    assert raises(LookupError, cache.put, "stays", 10) is first
    check(failures, [("first", 1, "expired"), ("second", value, "expired"), ("later", 3, "expired"),
                     ("stays", 9, "replaced")])
    check(cache.items(), [("stays", 10)])
    assert len(failures) == 4 and failures[1][1] is value
    for args, message in [(("", 0, 0), "key"), (("a", 0, True), "ttl"), (("a", 0, 0), "ttl")]:
        assert str(raises(ValueError, cache.put, *args)) == message
    stable = now
    for tick in (stable - 1, False, -1):
        now = tick
        assert str(raises(ValueError, cache.delete, "stays")) == "clock"
    now = stable
    check(cache.get("stays"), 10)
    for capacity, clock, callback in [(True, lambda: 0, None), (1, None, None), (1, lambda: 0, 4)]:
        assert str(raises(ValueError, Cache, capacity, clock, callback)) == "configuration"
    ticks = iter((0, 0, 1, 2))
    calls = []
    cache = Cache(3, lambda: next(ticks), lambda *event: calls.append(event))
    cache.put("a", "first", 2)
    cache.put("b", "second", 2)
    assert str(raises(ValueError, cache.get, "")) == "key"
    assert str(raises(ValueError, cache.delete, None)) == "key"
    check(cache.get("a"), "first")
    check(cache.items(), [])
    check(calls, [("b", "second", "expired"), ("a", "first", "expired")])
    error = LookupError("clock")
    cache = Cache(1, lambda: (_ for _ in ()).throw(error))
    assert raises(LookupError, cache.items) is error


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
