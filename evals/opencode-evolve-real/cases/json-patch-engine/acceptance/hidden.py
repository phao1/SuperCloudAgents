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
    'SPEC.md': '164e6525c46909d58e4b0194dbc6d97b32ce8f089a4771c1ab42579943ce92fa',
    'verify.py': 'd3704b9a3473eb4f15580153be8fee076ad33558dfaa48ef790fa8b655803bb3',
}


def check_feature():
    from copy import deepcopy

    from app import PatchError, apply

    for width in (1, 2, 3, 8, 14):
        for prefix in ("", "a/b", "~1", "🧭"):
            token = prefix.replace("~", "~0").replace("/", "~1")
            payload = {"p": [0]}
            values = list(range(width))
            doc = {prefix: values[:], "note": payload}
            ops = [
                {"op": "copy", "from": "/note", "path": f"/{token}/-"},
                {"op": "add", "path": f"/{token}/0", "value": payload},
                {"op": "move", "from": f"/{token}/1", "path": f"/{token}/{width + 1}"},
                {"op": "replace", "path": "/note/p/0", "value": -width},
                {"op": "test", "path": f"/{token}/0", "value": {"p": [0.0]}, "extra": None},
            ]
            before, original_ops = deepcopy(doc), deepcopy(ops)
            out = apply(doc, ops)
            check(out, {prefix: [payload, *values[1:], payload, 0], "note": {"p": [-width]}})
            out[prefix][0]["p"].append(42)
            check(out[prefix][-2], {"p": [0]})
            check(doc, before)
            check(ops, original_ops)
    for wrong, code in [
        (None, "operation"), ({"op": [], "path": ""}, "operation"),
        ({"op": "add", "path": "/", "from": "~"}, "operation"),
        ({"op": "copy", "path": "~", "from": None}, "operation"),
        ({"op": "copy", "path": "/missing/~", "from": "/never"}, "pointer"),
        ({"op": "copy", "path": "/a/xxx", "from": "missing"}, "pointer"),
        ({"op": "remove", "path": ""}, "root"),
        ({"op": "remove", "path": "/a/-"}, "missing"),
        ({"op": "add", "path": "/a/01", "value": 0}, "missing"),
        ({"op": "add", "path": "/a/２", "value": 0}, "missing"),
        ({"op": "add", "path": "/a/-1", "value": 0}, "missing"),
        ({"op": "add", "path": "/a/" + "9" * 5000, "value": 0}, "missing"),
        ({"op": "add", "path": "/a/0/new", "value": 0}, "missing"),
        ({"op": "replace", "path": "/a/1", "value": None}, "missing"),
        ({"op": "move", "from": "/a", "path": "/a/0"}, "descendant"),
        ({"op": "move", "from": "", "path": "/a"}, "descendant"),
        ({"op": "move", "from": "/none", "path": "/none"}, "missing"),
        ({"op": "test", "path": "/a", "value": [True]}, "test"),
    ]:
        source = {"a": [1], "z": 4}
        prior = {"op": "add", "path": "/new", "value": {"x": []}}
        error = raises(PatchError, apply, source, [prior, wrong])
        check((error.index, error.code), (1, code))
        check(source, {"a": [1], "z": 4})
        assert prior["value"] == {"x": []}
    for operation, expected in [
        ({"op": "move", "from": "", "path": ""}, {"a": [1], "-": {}, "": 0}),
        ({"op": "move", "from": "/a", "path": ""}, [1]),
        ({"op": "copy", "from": "", "path": "/-/01"}, {"a": [1], "-": {"01": {"a": [1], "-": {}, "": 0}}, "": 0}),
        ({"op": "replace", "path": "", "value": None}, None),
        ({"op": "add", "path": "", "value": 7}, 7),
        ({"op": "remove", "path": "/"}, {"a": [1], "-": {}}),
    ]:
        check(apply({"a": [1], "-": {}, "": 0}, [operation]), expected)
    before = {"~1": 1, "b": False, "a": None}
    check(list(apply(before, [{"op": "replace", "path": "/~01", "value": 3},
                              {"op": "add", "path": "/new", "value": 4}])), ["~1", "b", "a", "new"])
    merged = apply(before, [{"op": "add", "path": "/b", "value": {"current": True}}])
    check(list(merged.items()), [("~1", 1), ("b", {"current": True}), ("a", None)])
    check(apply({"values": ["x"]}, [{"op": "move", "path": "/values/0", "from": "/values/0"}]),
          {"values": ["x"]})
    result = apply(before, [{"op": "test", "path": "", "value": {"a": None, "b": False, "~1": 1.0}}])
    check(result, before)
    assert result is not before
    error = raises(PatchError, apply, {}, iter([]))
    check((error.index, error.code), (-1, "operation"))


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
