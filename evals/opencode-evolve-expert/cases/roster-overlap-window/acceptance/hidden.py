"""Independent acceptance checks for this fixture."""

import signal
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory


def check(actual, expected) -> None:
    assert type(actual) is type(expected) and actual == expected, (actual, expected)


def raises(error_type, call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except error_type as error:
        return error
    raise AssertionError(f"{call!r} did not raise {error_type.__name__}")


PROTECTED = {
    "README.md": "ecf0f510cb0770c06abad3fe7b400fae49a537e5e444257f2018c03496924d32",
    "docs/adr/012-desk-handoff.md": "ec98b6db7249dd93bdb9e75553508d580575c191d75e4f1c7660cf8762c41ac1",
    "docs/coverage.md": "dac629fcb03c6109ded2adda75093cbe76f55023eaa6d005bc3fb649381bb750",
    "docs/desk.md": "7b80fcf01293579781a34e6be026004955f43cd3c76d4acf0f15c6cf284cee8c",
    "docs/exports.md": "1c90b68086abae917d2beb0a29bbb57add11cb240d1909947840819952210b88",
    "docs/planning.md": "6a9872a52826288d4a1b671811777191ebb0146bd7b2d9aa92f088a34c440314",
    "tests/__init__.py": "a90921598a4accf8a3d10749aa0cde138004d60fd8020823664598ea2d296ea8",
    "tests/support.py": "98a717006203fb1cd9d981d9289e68a12ea7beb0e300e913aaf5e885e8b60f28",
    "tests/test_assignments.py": "ba849f4845a14a6fe6df34b4546d349c7eb66ad1a83a2f70334e5501d31fa631",
    "tests/test_coverage.py": "e5a0a435fa16042a4c6472e84600ee90c7d0962955f45718329c63a19454ecf4",
    "tests/test_exports.py": "d211d4a9b28490339bb7a9a4f8601cb7e71bc6ee918c79f9f0060ae8b14960a3",
    "tests/test_schedule.py": "e5f94edf97b591c4abba98f7b6065d33db69f6ed2e3dee9a79793ffca3a7880d",
    "tests/test_settings.py": "06602acb79ee4fa9f427702675b11e149620faa86e03578b11c91e26bc7751a5",
    "tests/test_workflows.py": "86f145c2f01e6ac48b905d5159a6cde9a2484b1b841f43de3b56f0c75c7674a1",
    "verify.py": "600a7b26704752c3e543c4346f989aebfbac5920bf273ebce522cd8f156d6945",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from crewcall.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--desk", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json

    from crewcall import assignments, settings
    from tests.support import MON, TUE, fixture

    path = root / "desk.json"
    desk = fixture(path)
    for rule, lane, dow, start, minutes, day in (
        ("a", "doors", "mon", "23:30", 180, MON),
        ("b", "mix", "tue", "00:45", 210, TUE),
        ("c", "doors", "tue", "01:30", 30, TUE),
        ("d", "doors", "tue", "02:15", 45, TUE),
        ("other", "mix", "tue", "02:00", 120, TUE),
        ("skipped", "doors", "tue", "00:50", 120, TUE),
    ):
        desk.change(settings.rule, rule, lane, dow, start, minutes, day)
        desk.change(assignments.add, f"{rule}:{day}", "pat" if rule == "other" else "ada")
    desk.change(settings.override, "skipped", TUE, skip=True)
    before = path.read_bytes()

    def review(low: str, high: str) -> list:
        code, out, error = cli(path, "roster", "conflicts", "ada", low, high)
        check((code, error), (0, ""))
        return [(row["first"], row["second"], row["start"], row["end"]) for row in json.loads(out)]

    want = [
        (f"b:{TUE}", f"a:{MON}", "00:40", "02:15"),
        (f"b:{TUE}", f"c:{TUE}", "01:30", "02:00"),
        (f"a:{MON}", f"c:{TUE}", "01:30", "02:00"),
        (f"a:{MON}", f"d:{TUE}", "02:15", "02:20"),
    ]
    check(review(TUE + "T00:40Z", TUE + "T02:20Z"),
          [(a, b, f"{TUE}T{left}:00Z", f"{TUE}T{right}:00Z") for a, b, left, right in want])
    check(review(TUE + "T02:30Z", TUE + "T03:00Z"), [])
    check(cli(path, "roster", "conflicts", "ada", TUE + "T02:30Z", TUE + "T02:30Z"),
          (2, "", "error: invalid review window\n"))
    check(path.read_bytes(), before)


def main() -> int:
    signal.setitimer(signal.ITIMER_REAL, 24)
    for name, digest in PROTECTED.items():
        path = Path(name)
        assert path.is_file() and not path.is_symlink(), name
        assert sha256(path.read_bytes()).hexdigest() == digest, name
    public = subprocess.run([sys.executable, "-B", "verify.py"], check=False, timeout=14)
    if public.returncode != 0:
        print("Public verification failed.", flush=True)
        return 1
    print("Public verification passed.", flush=True)
    try:
        with TemporaryDirectory(prefix="expert-acceptance-") as directory:
            feature(Path(directory))
    except AssertionError:
        print("Feature assertion failed.", flush=True)
        raise
    print("Feature checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
