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
    "docs/adr/012-desk-handoff.md": "3213f575da4711596747dd9abc30c13d505e316500b665c3a4679d17540f8560",
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
    "verify.py": "8c109bb624cc1214210e7c2d84da671dde1febae746f7fcb022c0022b0b7f998",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from crewcall.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--desk", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json

    from crewcall import assignments, payroll, settings
    from crewcall.desk import Desk
    from tests.support import fixture

    path = root / "desk.json"
    desk = fixture(path)
    before, change, after = "2024-10-26", "2024-10-27", "2024-10-28"
    desk.change(settings.rule, "overnight", "mix", "sat", "23:30", 180, before)
    desk.change(assignments.add, f"overnight:{before}", "ada")
    for offset, length in ((15, 40), (35, 45), (150, 15), (20, 10)):
        desk.change(assignments.record_break, f"overnight:{before}", "ada", offset, length)
    desk.change(settings.rule, "full", "mix", "sun", "00:00", 1500, change)
    desk.change(assignments.add, f"full:{change}", "pat")
    desk.change(settings.rule, "later", "mix", "sun", "23:40", 40, change)
    desk.change(assignments.add, f"later:{change}", "ada")
    desk.change(assignments.record_break, f"later:{change}", "ada", 36, 4)
    desk.change(settings.override, "later", change, minutes=37)
    desk.change(settings.rule, "elsewhere", "doors", "sun", "00:00", 15, change)
    desk.change(assignments.add, f"elsewhere:{change}", "lin")

    def report(*args: str) -> list:
        code, out, error = cli(path, "payroll", "report", *args)
        check((code, error), (0, ""))
        return [(row["day"], row["venue"], row["member"], row["minutes"]) for row in json.loads(out)]

    saved = path.read_bytes()
    check(report(before, change), [
        (before, "studio", "ada", 15), (change, "main", "lin", 15),
        (change, "studio", "ada", 105), (change, "studio", "pat", 1500),
    ])
    check(report(change, change, "--venue", "studio"),
          [(change, "studio", "ada", 105), (change, "studio", "pat", 1500)])
    check(report(after, after), [(after, "studio", "ada", 16)])
    check(path.read_bytes(), saved)

    spring = "2024-03-31"
    desk.change(settings.rule, "spring", "mix", "sun", "00:00", 1380, spring)
    desk.change(assignments.add, f"spring:{spring}", "ada")
    result, = payroll.report(Desk(path).state, spring, spring, "studio")
    check((result.member, result.minutes), ("ada", 1380))


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
