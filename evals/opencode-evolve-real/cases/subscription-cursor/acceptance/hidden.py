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
    'docs/delivery.md': '654815a4ecda81979edeb5d15726205c1d4db7883d255dae181b7713fdf47d52',
    'verify.py': '5ac212359c24501896ca68a5ee0017e200a6e1b7fa11883fa01cc0c1a2f99fee',
}


def check_feature():
    from itertools import product

    from feed import page
    from journal import Index, Record
    from subscription import collect

    for visible in (set(), {2}, {3, 11}, {2, 3, 7, 11}):
        rows = [Record(i, "entry" if i in visible else ("meta" if i % 2 else "seal"), str(i))
                for i in (2, 3, 7, 11)]
        index = Index(rows)
        for (lower, upper), budget in product(((0, 20), (3, 11), (2, 2), (13, 14)), (1, 2, 3, 8)):
            selected = [row for row in rows if lower < row.sequence <= upper]
            done = len(selected) <= budget
            cursor = upper if done else selected[budget - 1].sequence
            physical = selected[:budget]
            found = [row for row in physical if row.sequence in visible]
            check(index.scan(lower, upper, budget), (found, cursor, done))
            check(page(index, lower, upper, budget).cursor, cursor)
            check(collect(index, lower, upper, budget),
                  ([row.payload for row in selected if row.sequence in visible], upper))
    check(collect(Index([]), 1, 13, 2), ([], 13))


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
