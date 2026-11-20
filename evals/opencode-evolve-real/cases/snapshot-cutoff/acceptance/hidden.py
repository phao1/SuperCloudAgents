"""Hidden acceptance for this standalone fixture."""

import signal
import subprocess
import sys
from datetime import UTC
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
    'docs/snapshots.md': '9fc78d87c513351ca8cf7dee619d33aa1f8885b62bb75f5a68674ca07265c28f',
    'verify.py': '1892763ac4e1baba60aae0faf75a9949c7a3e90f4d739c00e321569d0dce6533',
}


def check_feature():
    from datetime import datetime, timedelta, timezone

    from keys import instant_key
    from readings import Reading, before
    from report import render

    for center, seconds in [(datetime(1970, 1, 1, tzinfo=UTC), 0),
                            (datetime(1969, 12, 31, tzinfo=UTC), -86400),
                            (datetime(2038, 1, 19, 3, 14, 8, tzinfo=UTC), 2**31),
                            (datetime(9990, 1, 1, tzinfo=UTC), 253086768000)]:
        for offset in (-720, 0, 345, 837):
            zone = timezone(timedelta(minutes=offset))
            boundary = center.astimezone(zone)
            for micro in (-1000, -1, 0, 1, 1000):
                check(instant_key(boundary + timedelta(microseconds=micro)), seconds * 10**6 + micro)
            rows = [Reading("cut", 0, boundary), Reading("later", 9, center + timedelta(microseconds=1)),
                    Reading("雪", 5, center - timedelta(microseconds=1)),
                    Reading("tie", 8, boundary - timedelta(microseconds=1))]
            saved = rows[:]
            check(before(rows, center), rows[2:])
            check(render(rows, boundary), "雪=5\ntie=8\n")
            check(rows, saved)
            assert str(raises(RuntimeError, render, rows, rows[2].at)) == "snapshot is empty"


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
