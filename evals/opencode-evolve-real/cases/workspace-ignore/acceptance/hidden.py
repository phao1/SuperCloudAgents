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
    'docs/contract.md': 'f743c049c06103ff43bbfdee10742e146a1130592efc498c3e63c2371dc0a079',
    'verify.py': '210c10497e509908c21a7accf41b84068b70176c52452bd4e21c2bbebbfe095a',
}


def check_feature():
    from itertools import product

    from app import Entry, included

    for name, leaf in product(("log", "雪", "!note", " note "), ("x", ".x", "\n", "🧭")):
        pathname = f"{name}/deep/{leaf}"
        entries = [Entry(pathname), Entry(name, True), Entry(pathname), Entry("other", True)]
        before = entries[:]
        token = r"\!" + name[1:] if name.startswith("!") else name
        check(included(entries, [f"/{token}/", f"!/{token}/**/{leaf}"]), ["other"])
        check(included(entries, [f"/{token}/**", f"!/{token}/", f"!/{token}/deep/", f"!/{token}/**/{leaf}"]),
              [pathname, name, pathname, "other"])
        check(included(entries, [f"/{token}/*/{leaf}", f"!/{token}/deep/"]), [name, "other"])
        check(entries, before)
    check(included([Entry("a"), Entry("x/a"), Entry("b"), Entry("x/a/child")], ["**/a"]), ["b"])
    check(included([Entry("a/**"), Entry("a/x"), Entry("a/right/child")], [r"a/\*\*"]), ["a/x", "a/right/child"])
    check(included([Entry("snow"), Entry("snow/X"), Entry("snow/x")], ["snow/*", "!snow/X"]), ["snow", "snow/X"])
    check(included([Entry("a\\b"), Entry("acb")], [r"a\\b"]), ["acb"])
    check(included([], ["**"]), [])


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
