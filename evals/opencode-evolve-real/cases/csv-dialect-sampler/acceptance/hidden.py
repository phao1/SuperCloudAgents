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
    'SPEC.md': '82c2611651465f131dc7bf85ef43c7e35ab758a1840c804de17e9db3393ce82d',
    'verify.py': '616f6665bfb0e9cbbdf722ad822e8043e0164e191963f73b08c2f012e01ceebe',
}


def check_feature():
    from app import CsvError, Dialect, records, sniff

    for delimiter in (",", ";", "\t", "|"):
        for newline in ("\r\n", "\n"):
            for bom in ("", "\ufeff"):
                sep = delimiter
                row1 = f'"a{sep}好"{sep}  z{sep}'
                row2 = f'"a""b"{sep}"\r\n\r\n♪"{sep}x'
                for last in ("", newline):
                    raw = bom + newline + row1 + newline * 2 + row2 + last
                    check(records(raw, sep), [[f"a{sep}好", "  z", ""], ['a"b', "\r\n\r\n♪", "x"]])
                    check(sniff(raw), Dialect(sep, 3, 2, newline))
        for suffix in ("\r\n", "\n", ""):
            raw = delimiter.join(["", "", "雪"]) + suffix
            check(records(raw, delimiter), [["", "", "雪"]])
        for offset, code, suffix in [
            (1, "quote", 'a"b'), (3, "trailing", '"雪"x'), (3, "newline", '"雪"\r'),
            (1, "newline", "x\ry"), (0, "unclosed", '"雪""'), (0, "unclosed", '"'),
        ]:
            for prefix in ("", "\ufeff" + delimiter + "\r\n", "\ufeff"):
                error = raises(CsvError, records, prefix + suffix, delimiter)
                assert (error.code, error.offset) == (code, offset + len(prefix))
    for text, dialect in [
        ("x,y|z\na,b|c", Dialect(",", 2, 2, "\n")),
        ("x,y|z|z\na,b|c|c", Dialect("|", 3, 2, "\n")),
        ("a,b|x\nb,c|x\na,b\n", Dialect(",", 2, 3, "\n")),
        ("a,b\na,b\na\na", None), ("a,b\n\r\na,b", Dialect(",", 2, 2, "\n")),
        ("a,b\r\na,b\r\none\n", Dialect(",", 2, 2, "\r\n")),
        ("a,b\r\nx\nc,d", Dialect(",", 2, 2, "\n")),
        ("a\r\nb,c\nb,c\r\nfoo\n", None),
        ("a\r\nb,c\nb,c\n", Dialect(",", 2, 2, "\n")),
        ('a,b\na,b\n"unfinished', None), ("", None), ("\ufeff\r\n", None),
    ]:
        check(sniff(text), dialect)
    check(records('\ufeff\ufeff,\n  \n""\n\nlast,', ","), [["\ufeff", ""], ["  "], [""], ["last", ""]])
    check(records('"";""\r\n"\r";  ', ";"), [["", ""], ["\r", "  "]])
    for bad in ("", ";;", " ", "\n"):
        assert str(raises(ValueError, records, "\r", bad)) == "delimiter"


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
