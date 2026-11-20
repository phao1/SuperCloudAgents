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
    'docs/contract.md': '1f9ca79760559b17ee9584e6aebc57219dd439166b8cad5e11b1b269c49798d4',
    'verify.py': '3753e3f3a84f32efabe93d335e35411288659deffb0e7ababed9855b79a499d3',
}


def check_feature():
    from fractions import Fraction
    from random import Random

    from app import Line, allocate

    rng = Random(831)
    keys = ("z", "a", "é", "é", " ", "雪", "aaa")
    for multiplier in (1, 7, 2**54 + 7):
        for scenario in range(25):
            lines = [Line(key, rng.randrange(10), rng.randrange(60) * multiplier) for key in keys]
            before = lines[:]
            amount = rng.randrange(300) * multiplier + scenario
            eligible = [line for line in lines if line.weight and line.cap]
            remaining = min(amount, sum(line.cap for line in eligible))
            effective = {line.code: Fraction(0) for line in lines}
            by_ratio = sorted(eligible, key=lambda line: Fraction(line.cap, line.weight))
            total_weight = sum(line.weight for line in eligible)
            for line in by_ratio:
                if line.cap * total_weight > remaining * line.weight:
                    break
                effective[line.code] = Fraction(line.cap)
                remaining -= line.cap
                total_weight -= line.weight
            if total_weight:
                for line in eligible:
                    if not effective[line.code]:
                        effective[line.code] = Fraction(line.weight * remaining, total_weight)
            expected = {name: share.numerator // share.denominator for name, share in effective.items()}
            target = min(amount, sum(line.cap for line in eligible))
            count = target - sum(expected.values())
            winners = sorted(keys, key=lambda key: effective[key] - expected[key], reverse=True)[:count]
            for key in winners:
                expected[key] += 1
            actual = allocate(amount, lines)
            check(actual, expected)
            check(list(actual), list(keys))
            assert all(type(value) is int for value in actual.values())
            assert lines == before
    for data in (None, (), [None], [Line("x", 1.5, 2)], [Line("x", 1, 1.0)], [Line("x", 0, 0)] * 2):
        assert str(raises(ValueError, allocate, 0, data)) == "allocation"
    assert str(raises(ValueError, allocate, -1, [])) == "allocation"


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
