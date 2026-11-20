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
    "docs/adr/012-desk-handoff.md": "640a3e07eb97604651019bda8a1e4f494e43b8508175fce30351c3582d2c3864",
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
    "verify.py": "b093a01268c5acb033cfccf3143784850ba10b71cf4327a8040d2ecabf4b0854",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from crewcall.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--desk", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json

    from crewcall import assignments, schedule, settings
    from crewcall.desk import Desk
    from tests.support import fixture

    path = root / "desk.json"
    desk = fixture(path)
    spring, fall = "2024-03-31", "2024-10-27"
    desk.change(settings.rule, "stage", "mix", "sun", "02:35", 80, spring)
    desk.change(settings.rule, "early", "doors", "sun", "01:30", 10, spring)
    desk.change(settings.override, "stage", fall, start="02:30", minutes=115)
    desk.change(assignments.add, f"stage:{fall}", "pat")
    code, out, error = cli(path, "roster", "show", spring, spring)
    check((code, error), (0, ""))
    check([(row["rule"], row["start"]) for row in json.loads(out)],
          [("stage", "2024-03-31T01:00:00Z"), ("early", "2024-03-31T01:30:00Z")])
    row = schedule.get(Desk(path).state, f"stage:{fall}")
    check((row.start.isoformat(), row.end.isoformat()), (
        "2024-10-27T01:30:00+00:00", "2024-10-27T03:25:00+00:00",
    ))
    code, wire, err = cli(path, "calendar", "export", fall, fall, "--at", "2024-10-26T10:00Z")
    check((code, err), (0, ""))
    assert f"UID:stage:{fall}@crewcall\r\n" in wire
    assert "DTSTART:20241027T013000Z\r\nDTEND:20241027T032500Z\r\n" in wire

    desk.change(settings.venue, "isle", "Island", "Australia/Lord_Howe")
    desk.change(settings.lane, "isle", "isle", "sound", "Radio")
    island_spring, island_fall = "2024-10-06", "2024-04-07"
    desk.change(settings.rule, "radio", "isle", "sun", "02:17", 25, island_fall)
    desk.change(settings.override, "radio", island_fall, start="01:50")
    for day, expected in [
        (island_spring, "2024-10-05T15:30:00Z"), (island_fall, "2024-04-06T15:20:00Z"),
    ]:
        code, out, err = cli(path, "board", "coverage", day, day, "--venue", "isle")
        check((code, err), (0, ""))
        check([(row["shift"], row["start"]) for row in json.loads(out)],
              [(f"radio:{day}", expected)])
    desk.change(settings.override, "radio", island_fall, skip=True)
    check(schedule.between(Desk(path).state, island_fall, island_fall, "isle"), [])

    desk.change(settings.venue, "main", "NY annex", "America/New_York")
    desk.change(settings.rule, "us", "doors", "sun", "01:25", 65, "2024-11-03")
    ny, = schedule.between(Desk(path).state, "2024-11-03", "2024-11-03", "main")[:1]
    check((ny.id, ny.start.isoformat()), ("us:2024-11-03", "2024-11-03T06:25:00+00:00"))


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
