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
    'docs/adr/006-aliases.md': '1ad6a47d2a80011066ef79751ad59dd97537fa00c9c62e360d22fd7b0fa65ac4',
    'docs/plans.md': '1314177c26f6ce8e169b35b0dc16f9a9f5c697aa27edf77a91b121fae3937e5a',
    'verify.py': 'c7ca4175c8aa91a0782a03ff57b474cde23e3ccd2cec7ab7bb7952cbf0ec295a',
}


def check_feature():
    from copy import deepcopy

    from app import build
    from graph import Package

    for index in range(12):
        version = "" if index % 2 else f"雪-{index}"
        catalog = {"all": Package("1", ["api-alt", "site", "?lookup", "?missing", "?unused"]),
                   "api": Package(version, ["common", "?absent"]), "site": Package("3", ["common"]),
                   "common": Package("v4", [])}
        aliases = {"api-alt": "api-mid", "api-mid": "api", "common": "ignored",
                   "lookup": "missing", "unused": "absent", "bad": "bad"}
        before = deepcopy(catalog)
        check(build(["all", "api-mid"], catalog, aliases), {
            "packages": [{"name": name, "version": catalog[name].version, "selected_by": parent}
                         for name, parent in [("common", "api"), ("api", "all"), ("site", "all"), ("all", None)]],
            "skipped": [{"parent": "api", "name": "absent"}, {"parent": "all", "name": "missing"},
                        {"parent": "all", "name": "absent"}],
        })
        check(build(["common", "api-alt"], catalog, aliases)["packages"], [
            {"name": "common", "version": "v4", "selected_by": None},
            {"name": "api", "version": version, "selected_by": None},
        ])
        check(catalog, before)
    check(build([], {}, {"bad": "bad"}), {"packages": [], "skipped": []})
    for catalog, aliases, target, message in [
        ({}, {"start": "missing"}, "start", "missing: missing"),
        ({}, {"start": "a", "a": "b", "b": "a"}, "start", "alias cycle: a -> b -> a"),
        ({"root": Package("", ["?loop"])}, {"loop": "loop"}, "root", "alias cycle: loop -> loop"),
        ({"root": Package("", ["a"]), "a": Package("", ["b"]), "b": Package("", ["x"])},
         {"x": "a"}, "root", "dependency cycle: a -> b -> a"),
        ({"root": Package("", ["?a", "later"]), "a": Package("", ["lost"])},
         {"later": "later"}, "root", "missing: lost"),
    ]:
        assert str(raises(ValueError, build, [target], catalog, aliases)) == message


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
