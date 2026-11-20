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
    'docs/contract.md': '7b7063709e0b575f805b2642429f1cc5f2de07230c81e62352557780f3f43e99',
    'verify.py': 'ccfb864ceeaf56d5d456cb5d5eefb8ad91b1e5035e0ff092aeffbb46cfe3a7c7',
}


def check_feature():
    from itertools import groupby
    from random import Random

    from app import Segment, compact

    rng = Random(602)
    for size in range(1, 12):
        for minimum in (1, 2, 4, 8):
            source = []
            for _ in range(size):
                left = rng.randrange(-8, 8)
                source.append(Segment(left, left + rng.randrange(1, 9),
                                      rng.choice(["", "雪", "é", "é", None]), rng.randrange(-2, 3)))
            immutable = source[:]
            for offset in (0, 2**55):
                expected = []
                slots = []
                for tick in range(-6, 10):
                    choices = [(event.priority, i, event.label) for i, event in enumerate(source)
                               if event.start <= tick < event.stop]
                    slots.append((tick, max(choices)[-1] if choices else None))
                for label, group in groupby(slots, key=lambda item: item[1]):
                    ticks = [item[0] for item in group]
                    if label is not None and len(ticks) >= minimum:
                        expected.append((ticks[0] + offset, ticks[-1] + offset + 1, label))
                shifted = (Segment(i.start + offset, i.stop + offset, i.label, i.priority) for i in source)
                check(compact(shifted, offset - 6, offset + 10, minimum), expected)
            check(source, immutable)
    check(compact([Segment(-10**12, 10**12, "a"), Segment(0, 5, None, 1)],
                  -10**12, 10**12), [(-10**12, 0, "a"), (5, 10**12, "a")])

    def untouched():
        raise AssertionError("input should not be read")
        yield
    for values in [(False, 3, 1), (3, 4, True), (10, 0, 1), (0, 1, -1)]:
        assert str(raises(ValueError, compact, untouched(), *values)) == "window"
    for value in (None, Segment(1, 1, None), Segment(-9, -8, "x", False), Segment(1, 2, 2)):
        assert str(raises(ValueError, compact, iter([Segment(0, 1, "x"), value]), 0, 0)) == "segment 1"


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
