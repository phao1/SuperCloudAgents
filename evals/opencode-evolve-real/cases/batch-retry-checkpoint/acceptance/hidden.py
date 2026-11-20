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
    'docs/checkpoints.md': '12e450c8b92b1ff89a3720eb938082cb9c96b82aeaabf2af7da63d72379550c9',
    'docs/replay.md': '5dae581ca94d81384ef1c6826c37fdcd41bf39f2bc9e99aaa220329a92e8cc67',
    'verify.py': 'd5025f8b79f14f9514e23b771aefa2d1a0eb51264f23ba3c63b9c4cb459c42d2',
}


def check_feature():
    import json

    from checkpoint import dump, load
    from delivery import confirm
    from runner import Runner
    from storage import Storage

    for splinter in ("x", "雪", " a ", "é", "e\u0301", "/"):
        legacy = '{"version":1,"applied":{"old/z":7,"p/q":0}}'
        storage = Storage(legacy)
        entries = [("a/" + splinter, "z", -7), ("a", splinter + "/z", 10),
                   ("old", "z", 7), ("a", "/", 2)]
        expected = {("old", "z"): 7, ("p", "q"): 0}
        expected.update({(left, right): value for left, right, value in entries})
        check(confirm(Runner(storage), entries), [-7, 10, 7, 2])
        check(json.loads(storage.text), {"version": 2, "applied": [[*pair, value]
                                                                  for pair, value in expected.items()]})
        restarted = Storage(storage.text)
        check(confirm(Runner(restarted), entries[::-1]), [2, 7, 10, -7])
        check(list(load(restarted.text)), list(expected))
        check(load(dump(expected)), expected)
        saved = restarted.text
        assert str(raises(ValueError, confirm, Runner(restarted), [("new", "id", 8), ("p", "q", 4)])) == (
            "conflict"
        )
        check(restarted.text, saved)
    for malformed in [
        "", "[]", '{"version":true,"applied":{}}', '{"version":1,"applied":{"a/b/c":1}}',
        '{"version":1,"applied":{"a/":0}}', '{"version":1,"applied":{"a/b":false}}',
        '{"version":1,"applied":{"a":3}}', '{"version":1,"applied":{"a/b":0,"a/b":1}}',
        '{"version":2,"version":2,"applied":[]}', '{"version":2,"applied":{},"extra":[]}',
        '{"version":3,"applied":[]}', '{"version":2,"applied":[["x","y",1],["x","y",1]]}',
        '{"version":2,"applied":[["","y",1]]}', '{"version":2,"applied":[["x","y",1.0]]}',
        '{"version":2,"applied":[["x","y",1,2]]}', '{"version":2,"applied":[true]}',
    ]:
        assert str(raises(ValueError, load, malformed)) == "checkpoint"
    check(load('{"version":2,"applied":[]}'), {})


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
