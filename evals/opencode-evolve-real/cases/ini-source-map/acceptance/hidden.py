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
    'SPEC.md': '6bab87c98d2d65e7dda442c90bad494142bfce90e886a98c8ce0b5ce85bca11a',
    'verify.py': 'e227262060970e34b2085dc7d58ab58d5ee205ac4231bc09b6b7010197aea42b',
}


def check_feature():
    from app import IniError, parse

    forms = [
        ("", ""), (" ; note", ""), (" # note", ""), ("literal#x;y", "literal#x;y"),
        ("雪 ; note", "雪"), ('"a;#= 雪"\t; note', "a;#= 雪"),
        (r'"a\t\n\"\\z"', 'a\t\n"\\z'),
        ("x\\\\", "x\\\\"), ("x\u00a0 ", "x\u00a0"),
    ]
    for ending in ("\n", "\r\n"):
        for index, (raw, value) in enumerate(forms):
            name = f"key_{index}"
            source = ending.join(["# heading", "\tMiddle = " + raw, " [ A ]#comment",
                                  " " + name + "=" + raw, "[B]", "second=3", "[A]", "end=4"])
            parsed = parse(source)
            check(parsed, {"": {"Middle": (value, 2, 2)},
                           "A": {name: (value, 4, 2), "end": ("4", 8, 1)}, "B": {"second": ("3", 6, 1)}})
            check(list(parsed), ["", "A", "B"])
            check(list(parsed["A"]), [name, "end"])
        for trailing in (" [X]", " # comment", '  "quoted"', " b=c"):
            tail = trailing.lstrip(" \t")
            source = ending.join(["v=one\\\\\\ # comment", trailing, "next=ok", ""])
            check(parse(source), {"": {"v": ("one\\\\\n" + ("" if tail[0] == "#" else tail), 1, 1),
                                      "next": ("ok", 3, 1)}})
        check(parse(ending.join(["v=  \\", "  \\ ; comment", "  ]x", "x=1"])),
              {"": {"v": ("\n\n]x", 1, 1), "x": ("1", 4, 1)}})
    failures = [
        ("  [broken", "section", 3), ("\t[é]", "section", 2), ("[S] +", "trailing", 5),
        (" word ", "assignment", 2), ("  0x=1", "key", 3), ("  =1", "key", 3),
        ('x = "雪" x', "trailing", 9), ('x = "雪', "quote", 5),
        ('x = "雪\\q"', "escape", 7), ('x = "雪\\', "escape", 7),
        ("x=字\\ # comment", "continuation", 4), ("; comment\r.", "bare-cr", 10),
        ("x\u00a0=1", "key", 1), (";\r", "bare-cr", 2),
    ]
    for line_prefix in ("", "# one\r\n", "\n\n"):
        for case, code, column in failures:
            error = raises(IniError, parse, line_prefix + case)
            assert (error.code, error.line, error.column) == (code, line_prefix.count("\n") + 1, column)
    for case, location in [
        ("[R]\nx=1\n[S]\nx=2\n[R]\n x=\"unterminated", ("duplicate", 6, 2)),
        ("x=雪\\\n \rX", ("bare-cr", 2, 2)), ("x=雪\\\n\t", ("continuation", 1, 4)),
        ("x=雪\\\ncomment=yes", ("continuation", 1, 4)),
    ]:
        error = raises(IniError, parse, case)
        check((error.code, error.line, error.column), location)
    check(parse(""), {"": {}})
    check(parse("name =\nName=x\n"), {"": {"name": ("", 1, 1), "Name": ("x", 2, 1)}})


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
