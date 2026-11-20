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
    "docs/adr/012-desk-handoff.md": "462f51c0cf92b8328991b34661305ea47aa5e2cd91fbc84289ad345af6d6fc52",
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
    "verify.py": "707fab5006281beb9f23305f0c8dec2037ad8b2ad720f39fd7681fcbe459eb56",
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
    from crewcall.desk import Desk
    from tests.support import MON, TUE, fixture

    path, sheet = root / "desk.json", root / "substitutes.json"
    desk = fixture(path)
    desk.change(settings.member, "mei", "Mei", ["door"])
    desk.change(settings.rule, "front", "doors", "mon,tue", "09:00", 60, MON)
    today, later = f"front:{MON}", f"front:{TUE}"
    for shift, member, offset, duration in (
        (today, "ada", 8, 10), (today, "lin", 22, 5), (later, "lin", 0, 60),
    ):
        desk.change(assignments.add, shift, member)
        desk.change(assignments.record_break, shift, member, offset, duration)
    desk.change(assignments.record_break, today, "ada", 8, 10)

    def change(rows) -> tuple[int, str, str]:
        sheet.write_text(json.dumps(rows, ensure_ascii=False))
        return cli(path, "roster", "substitute", str(sheet))

    def row(source, target, shift=today):
        return {"shift": shift, "from": source, "to": target}

    swap = [row("lin", "ada"), row("ada", "lin")]
    code, output, error = change(swap)
    check((code, error), (0, ""))
    check(json.loads(output), swap)
    state = Desk(path).state
    check(state.rules, desk.state.rules)
    check([(entry.shift, entry.member, entry.offset, entry.minutes) for entry in state.breaks], [
        (today, "ada", 22, 5), (today, "lin", 8, 10), (today, "lin", 8, 10),
        (later, "lin", 0, 60),
    ])
    before = path.read_bytes()
    for changes, message in (
        ([row("lin", "ada")], "already assigned"),
        ([row("ada", "mei"), row("lin", "pat")], "member lacks shift role"),
        ([row("ada", "mei"), row("ada", "lin")], "duplicate substitution source"),
        ([row("ada", "mei"), row("lin", "mei")], "duplicate substitution destination"),
        ([row("ada", "mei"), row("ada", "lin", later)], "unknown assignment"),
        ([row("ada", "mei"), row("lin", False)], "invalid substitution file"),
    ):
        check(change(changes), (2, "", f"error: {message}\n"))
        check(path.read_bytes(), before)
    check(change([]), (0, "[]\n", ""))
    check(change([row("ada", "ada")])[0], 0)
    check(path.read_bytes(), before)

    chain = [row("ada", "lin"), row("lin", "mei"), row("lin", "ada", later)]
    check(change(chain)[0], 0)
    state = Desk(path).state
    check(assignments.crew(state, today), ["lin", "mei"])
    check(assignments.crew(state, later), ["ada"])
    check([(row.shift, row.member, row.offset) for row in state.breaks], [
        (today, "lin", 22), (today, "mei", 8), (today, "mei", 8), (later, "ada", 0),
    ])


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
