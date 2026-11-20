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
    'docs/transfers.md': '7fef5b1251acd9001eb50dee7fc7bd53531129239bcc4eec81abc4c4fc9a9221',
    'verify.py': '04e396c2e7cf2a4a250786623151addc3c876663c36ee4c60f416e03e82df13c',
}


def check_feature():
    from changes import compact
    from moves import Move, apply
    from receipt import issue
    from stock import Stock

    for sku in ("x", "雪", "a:b", ""):
        for units in (1, 2, 17, 2**60):
            first, middle, last = "first", "中", "a:b"
            initial = {(first, sku): units}
            stock = Stock(initial)
            moves = [Move(middle, last, sku, units), Move(first, middle, sku, units)]
            check(apply(stock, moves), [(last, sku, units), (first, sku, -units)])
            check(stock.quantity(middle, sku), 0)
            check(stock.quantity(last, sku), units)
            check(stock.quantity(first, sku), 0)
            check(initial, {(first, sku): units})
            assert str(raises(ValueError, apply, stock, [
                Move(last, middle, sku, units), Move(last, first, sku, 1),
            ])) == "insufficient stock"
            check(stock.quantity(last, sku), units)
            check(stock.quantity(middle, sku), 0)
            check(stock.quantity(first, sku), 0)
            check(compact(iter([(first, sku, 0), (middle, sku, -units), (first, sku, units)])),
                  [(first, sku, units), (middle, sku, -units)])
    check(compact(iter([("a", "a:b", 2), ("a:a", "b", 3), ("a", "a:b", -1)])),
          [("a", "a:b", 1), ("a:a", "b", 3)])
    check(compact(iter([("Z", "Y", 0), ("A", "X", 3), ("Z", "Y", 4), ("Z", "X", 1)])),
          [("Z", "Y", 4), ("A", "X", 3), ("Z", "X", 1)])
    same = Stock({("a", "x"): 1})
    assert str(raises(RuntimeError, issue, same, [Move("a", "a", "x", 1)])) == "no inventory changes"
    check(same.quantity("a", "x"), 1)


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
