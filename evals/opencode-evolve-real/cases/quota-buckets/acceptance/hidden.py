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
    'SPEC.md': 'fc5b9d21f69826cdfb2aac79f6866d9c18aa5588cf7fc51a95364470586d6fde',
    'verify.py': '29a8a190fdc757d681b5cd6bea3c0dba28e003d3794f0514783d11dd2214a603',
}


def check_feature():
    from fractions import Fraction
    from math import ceil, floor
    from random import Random

    from app import Buckets, Decision

    class Clock:
        def __init__(self, tick):
            self.tick = tick
            self.reads = []

        def __call__(self):
            self.reads.append(self.tick)
            return self.tick

    for capacity, refill, period in [(1, 1, 2), (4, 3, 7), (3, 7, 2), (5, 2, 3), (2**60 + 1, 1, 2**55 + 3)]:
        clock = Clock(2**55)
        bucket = Buckets(capacity, refill, period, clock)
        assert clock.reads == []
        rate = Fraction(refill, period)
        credits = {key: Fraction(capacity) for key in ("ingest", "雪", "write")}
        rng = Random(capacity)
        for step in range(26):
            delta = rng.choice([0, 1, 1, 2, 15, 90])
            clock.tick += delta
            credits = {key: min(capacity, credit + delta * rate) for key, credit in credits.items()}
            selection = rng.sample(list(credits), step % 3 + 1)
            costs = {key: rng.choice([1, capacity, max(1, capacity - 1)]) for key in selection}
            commit = step % 5 != 2
            retry = max(ceil((costs[key] - credits[key]) / rate) for key in selection)
            if retry <= 0 and commit:
                for key in selection:
                    credits[key] -= costs[key]
            expected = Decision(retry <= 0, clock.tick + retry if retry > 0 else None,
                                {key: floor(credits[key]) for key in selection})
            unchanged = costs.copy()
            output = bucket.attempt(costs, commit=commit)
            check(output, expected)
            assert list(output.balances) == selection
            assert clock.reads == sorted(clock.reads) and len(clock.reads) == step + 1
            assert costs == unchanged
            output.balances.clear()

    ticks = iter((8, 7, False, -1, 9, 10))
    bucket = Buckets(1, 1, 2, lambda: next(ticks))
    for costs, commit in [(None, True), ({}, True), ({"": 1}, True), ({1: 1}, True),
                          ({"a": True}, True), ({"a": 2}, True), ({"a": 0}, True), ({"a": 1}, 1)]:
        assert str(raises(ValueError, bucket.attempt, costs, commit=commit)) == "costs"
    check(bucket.attempt({"a": 1}), Decision(True, None, {"a": 0}))
    for _ in range(3):
        assert str(raises(ValueError, bucket.attempt, {"a": 1})) == "clock"
    check(bucket.attempt({"a": 1}), Decision(False, 10, {"a": 0}))
    check(bucket.attempt({"a": 1}, commit=False), Decision(True, None, {"a": 1}))
    for args in [(True, 1, 1, lambda: 0), (1, 0, 1, lambda: 0), (1, 1, False, lambda: 0), (1, 1, 1, 0)]:
        assert str(raises(ValueError, Buckets, *args)) == "configuration"

    error = LookupError("injected")
    def broken():
        raise error

    independent = Buckets(1, 1, 1, broken)
    assert raises(LookupError, independent.attempt, {"a": 1}) is error
    check(Buckets(1, 1, 1, lambda: 0).attempt({"a": 1}), Decision(True, None, {"a": 0}))


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
