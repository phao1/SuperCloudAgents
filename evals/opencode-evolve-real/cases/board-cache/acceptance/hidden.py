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
    'docs/preview.md': 'de1d5096189f40facc9acfdb04248336bfe2197a1fd9f8c0a651dcd56330398f',
    'verify.py': 'f2bf99e31b03d905f775d413d80a9012c297ccc0f5c041c8fd35a420d38f9cdf',
}


def check_feature():
    from dataclasses import replace
    from itertools import product

    from memo import Memo
    from models import Item, Snapshot
    from panel import draw
    from preview import Preview

    memo = Memo()
    seen = []
    for slug, revision, tag, descending, limit in product(
        ("a", "ab"), (0, 1), (None, "", "É", "é", "e\u0301"), (False, True), (None, 0, 1),
    ):
        snapshot = Snapshot(slug, revision, ())
        query = (tag, descending, limit)
        value = {slug: query}

        def build(value=value):
            seen.append(value)
            return value

        assert memo.fetch(snapshot, *query, build) is value
        assert memo.fetch(replace(snapshot), *query, lambda: None) is value
    check(len(seen), 120)
    items = (Item("Z", "É"), Item("A", "É"), Item("O", "é"), Item("E", ""), Item("Q", "e\u0301"))
    preview = Preview()
    snapshot = Snapshot("test", 0, items)
    check(draw(preview, snapshot), "A\nE\nO\nQ\nZ")
    check(draw(preview, snapshot, tag="É", descending=True, limit=1), "Z")
    check(draw(preview, snapshot, tag="é"), "O")
    check(draw(preview, snapshot, tag=""), "E")
    check(draw(preview, snapshot, tag="e\u0301"), "Q")
    check(draw(preview, snapshot, limit=0), "")
    check(draw(preview, replace(snapshot, revision=1, items=())), "")
    check(draw(Preview(), replace(snapshot, items=())), "")


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
