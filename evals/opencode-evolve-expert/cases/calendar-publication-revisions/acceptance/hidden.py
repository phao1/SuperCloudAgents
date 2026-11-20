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
    "docs/adr/012-desk-handoff.md": "dd1533ba6a3795d9a2591a1529b3d1a6ce7d2958f2afcc14028108c85107e4f7",
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
    "verify.py": "e5c97ae4b7a31129b6433603c33b2cf9f6e397cb56256ff72be6dfa4078c64f4",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from crewcall.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--desk", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def entries(wire: str) -> list[dict]:
    assert wire.endswith("\r\n") and "\n" not in wire.replace("\r\n", "")
    assert all(len(line.encode("utf-8")) <= 75 for line in wire.split("\r\n"))
    unfolded = wire.replace("\r\n ", "")
    assert unfolded.startswith("BEGIN:VCALENDAR\r\n")
    assert unfolded.endswith("END:VCALENDAR\r\n")
    return [
        dict(line.split(":", 1) for line in event.split("\r\n") if line)
        for event in unfolded.split("BEGIN:VEVENT\r\n")[1:]
        for event in [event.split("END:VEVENT\r\n", 1)[0]]
    ]


def feature(root: Path) -> None:
    from crewcall import settings
    from tests.support import MON, TUE, fixture

    path = root / "desk.json"
    desk = fixture(path)
    desk.change(settings.rule, "rehearsal", "doors", "mon,tue", "17:00", 70, MON)
    desk.change(settings.rule, "temp", "doors", "mon", "14:00", 30, MON)
    desk.change(settings.override, "temp", MON, skip=True)

    def publish(first: str, last: str, at: str) -> list[dict]:
        code, out, error = cli(path, "calendar", "sync", first, last, "--at", at)
        check((code, error), (0, ""))
        return entries(out)

    at = MON + "T10:00Z"
    before = path.read_bytes()
    original = publish(MON, TUE, at)
    check([(row["UID"], row["SEQUENCE"], row.get("STATUS")) for row in original], [
        (f"rehearsal:{MON}@crewcall", "0", None), (f"rehearsal:{TUE}@crewcall", "0", None),
    ])
    check(path.read_bytes(), before)
    one, = publish(TUE, TUE, MON + "T12:14Z")
    check((one["DTSTAMP"], one["SEQUENCE"]), ("20240506T121400Z", "0"))
    label = 'Porte, salle; \\ bureau\n' + "音🎺" * 27
    escaped = r"Porte\, salle\; \\ bureau\n" + "音🎺" * 27
    desk.change(settings.lane, "doors", "main", "door", label)
    renamed, = publish(MON, MON, MON + "T12:55Z")
    check((renamed["UID"], renamed["SUMMARY"], renamed["SEQUENCE"]),
          (original[0]["UID"], escaped, "1"))
    desk.change(settings.override, "rehearsal", MON, skip=True)
    cancelled, = publish(MON, MON, at)
    check((cancelled["UID"], cancelled["STATUS"], cancelled["SEQUENCE"], cancelled["SUMMARY"],
           cancelled["DTSTART"], cancelled["DTEND"]),
          (renamed["UID"], "CANCELLED", "2", escaped, "20240506T170000Z", "20240506T181000Z"))
    check(publish(MON, MON, at)[0]["SEQUENCE"], "2")
    both = publish(MON, TUE, at)
    check([(row["UID"], row["SEQUENCE"]) for row in both],
          [(original[1]["UID"], "1"), (original[0]["UID"], "2")])
    desk.change(settings.override, "rehearsal", MON, start="18:00")
    restored, = publish(MON, MON, at)
    check((restored["UID"], restored.get("STATUS"), restored["SEQUENCE"], restored["DTSTART"]),
          (renamed["UID"], None, "3", "20240506T180000Z"))

    manifest = path.with_name(path.name + ".calendar.json")
    saved = manifest.read_bytes()
    code, out, error = cli(path, "calendar", "export", MON, MON, "--at", at)
    check((code, error), (0, ""))
    check(entries(out)[0].get("SEQUENCE"), None)
    check(manifest.read_bytes(), saved)
    check(cli(path, "calendar", "sync", TUE, MON, "--at", at),
          (2, "", "error: invalid reporting range\n"))
    check(manifest.read_bytes(), saved)


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
