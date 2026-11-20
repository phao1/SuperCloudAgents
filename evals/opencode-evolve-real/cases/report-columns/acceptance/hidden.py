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
    'docs/adr/001-wire-formats.md': 'fae43a2baaf33d88714dcaaa32d55c04e683b0a71c374eb88f8e24093443bf07',
    'docs/reports.md': '1653bad19641de4e538363a172199674c1025f05e75c005ae594462550abec70',
    'verify.py': 'cd82329a86cc0b61de0181d5eb996e066731a10815166d7c39e7a1ce859d71ee',
}


def check_feature():
    from copy import deepcopy

    from exports import csv_text, json_text
    from projection import Report, project

    rows = [{"id": "Å", "title": None, "count": -1, "note": 'a"b'}, {"id": "", "count": 0, "note": "\r"},
            {"id": ",", "title": "雪\n町", "count": 2}]
    original = deepcopy(rows)
    check(project(rows), Report(("ID", "Title", "Count"), (("Å", "", -1), ("", "", 0), (",", "雪\n町", 2)),
                               ("row 2: missing title",)))
    columns = ["name", "key", "total", "note"]
    check(project(rows, columns, "zh"), Report(("标题", "编号", "数量", "备注"),
            (("", "Å", -1, 'a"b'), ("", "", 0, "\r"), ("雪\n町", ",", 2, "")), ("row 2: missing title",)))
    check(csv_text(rows, ["note"]), 'Note\n"a""b"\n"\r"\n""\n')
    check(csv_text(rows, ["name", "key"]), 'Title,ID\n,Å\n,\n"雪\n町",","\n')
    check(csv_text(rows, []), "\n" * 4)
    check(json_text(rows, [], "zh"), '{"headers":[],"rows":[[],[],[]],"warnings":[]}\n')
    check(json_text(rows, ["name", "total"], "zh"),
          '{"headers":["标题","数量"],"rows":[["",-1],["",0],["雪\\n町",2]],"warnings":["row 2: missing title"]}\n')
    check(json_text([], ["note"]), '{"headers":["Note"],"rows":[],"warnings":[]}\n')
    check(csv_text([], ["note"]), "Note\n")
    check(project([{"note": None, "id": []}], ["note"]), Report(("Note",), (("",),), ()))
    check(rows, original)
    check(columns, ["name", "key", "total", "note"])
    for entry in (project, csv_text, json_text):
        for selection in (["name", "title"], ["total", "count"], ["ID"], ["foo"], ["note", "note"]):
            assert str(raises(ValueError, entry, rows, selection)) == "invalid columns"
        assert str(raises(ValueError, entry, rows, ["garbage"], "jp")) == "invalid locale"
        for invalid, selection, message in [
            ({}, ["key"], "row 1: id"), ({"id": None}, ["id"], "row 1: id"),
            ({"count": False}, ["total"], "row 1: count"), ({"count": 1.0}, ["count"], "row 1: count"),
            ({"title": []}, ["title"], "row 1: title"), ({"note": 0}, ["note"], "row 1: note"),
        ]:
            assert str(raises(ValueError, entry, [invalid], selection)) == message


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
