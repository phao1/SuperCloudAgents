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
    'docs/sampling.md': '6fb2dab22663354af16da3f78b61bcf502608ccdee91bdb6b3b101df60f8757f',
    'verify.py': '30befd40df9d9509f9f07f761e34e7e4bf0561bd276d7661db2172769e3da95a',
}


def check_feature():
    from itertools import product
    from random import Random

    from dashboard import panel
    from history import Sample, normalize
    from window import measure

    stations = ("snow", "Snow", "北", " ")
    rng = Random(7)
    arrivals = [Sample(rng.choice(stations), rng.randint(-6, 6), rng.choice([None, 0, -4, 5]))
                for _ in range(64)]
    for station, start, duration in product(stations, (-8, -1, 0, 4), (0, 1, 5, 12)):
        values = []
        for tick in range(start, start + duration):
            candidates = [(event.tick, pos, event.value) for pos, event in enumerate(arrivals)
                          if event.station == station and event.tick <= tick]
            if candidates:
                values.append(max(candidates)[-1])
        used = [value for value in values if value is not None]
        check(measure(iter(arrivals), station, start, start + duration), (len(used), sum(used)))
        if used:
            check(panel(iter(arrivals), station, start, start + duration),
                  f"{station}={sum(used)}/{len(used)}")
        else:
            assert str(raises(RuntimeError, panel, iter(arrivals), station, start, start + duration)) == (
                "no station coverage"
            )
    raw = [Sample("x", 3, 2), Sample("y", 3, 2), Sample("x", 3, None), Sample("z", -100, 0)]
    actual = normalize(iter(raw))
    expected = [raw[3], raw[1], raw[2]]
    check(actual, expected)
    assert all(left is right for left, right in zip(actual, expected))


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
