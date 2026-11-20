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
    'docs/adr/004-zones.md': '8cb29a9be4c2ae3358b21c5906854fe6a8268af20c9be878880ab2490f88e9cd',
    'docs/quotes.md': '1b07d1ce27483df49b03f096f2901c2ab59b8f953a0ed05682f626cc14434ff3',
    'verify.py': '7a60d5398b09ca606ac5dfda214dd3501edbae2fb78a900535e3da05577b18af',
}


def check_feature():
    import json
    from copy import deepcopy

    from service import quote

    for raw, name, base, rate, fee in [
        (" AA-1\t ", "metro", 100, 25, 75), ("AA0", "local", 120, 30, 75),
        (" b1--x ", "regional", 175, 40, 100), ("9-aa1", "remote", 250, 65, 125),
    ]:
        for grams, blocks in [(1, 1), (499, 1), (500, 1), (501, 2), (9999, 20), (10000, 20)]:
            pairs = [(1, True), (2, True), (3, False)]
            parcels = [{"grams": grams, "quantity": qty, "fragile": f} for qty, f in pairs]
            shipping = sum((base + rate * blocks) * qty for qty, _ in pairs)
            handling = sum(fee * qty for qty, f in pairs if f)
            discount = shipping // 10 if shipping >= 1000 else 0
            expected = {"zone": name,
                        "lines": [{"shipping_cents": (base + rate * blocks) * qty,
                                   "handling_cents": fee * qty if f else 0} for qty, f in pairs],
                        "discount_cents": discount, "total_cents": shipping + handling - discount}
            old = deepcopy(parcels)
            check(quote(raw, parcels), expected)
            check(parcels, old)
        check(quote(raw, []), {"zone": name, "lines": [], "discount_cents": 0, "total_cents": 0})
    check(quote("aa1", [{"grams": 1, "quantity": 7, "fragile": True}])["discount_cents"], 0)
    check(quote("aa1", [{"grams": 1, "quantity": 8}])["discount_cents"], 100)
    for bad in (True, "", "-a", "₆/₅", "ı2", "ſx", "B\n2", "AAAAAAA"):
        assert str(raises(ValueError, quote, bad, False)) == "postcode"
    for wrong in (None, {"grams": 2}, [{}], [True], [{"grams": True}], [{"grams": 10001}],
                  [{"grams": 1, "quantity": 0}], [{"grams": 1, "quantity": 51}],
                  [{"grams": 1, "quantity": True}], [{"grams": 1, "fragile": 0}],
                  [{"grams": 1, "comment": ""}]):
        assert str(raises(ValueError, quote, "AA", wrong)) == "parcels"
    command = [sys.executable, "-B", "-m", "cli"]
    for raw in ("{", "null", "[]", '{"postcode":"aa"}', '{"postcode":"XXX","parcels":[],"more":0}',
                '{"postcode":"x","parcels":[]}'):
        result = subprocess.run(command, input=raw, text=True, capture_output=True, check=False, timeout=2)
        check((result.returncode, result.stdout, result.stderr), (2, "", "invalid quote\n"))
    request = {"postcode": " b3 ", "parcels": [{"grams": 501, "quantity": 2, "fragile": True}]}
    result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True, check=False, timeout=2)
    check(result.returncode, 0)
    check(result.stderr, "")
    assert result.stdout.endswith("\n")
    check(json.loads(result.stdout), quote(**request))


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
