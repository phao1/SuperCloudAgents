"""Existing regressions and the issue's local reproduction."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from crewcall.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--desk", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def smoke(root: Path) -> None:
    from crewcall import settings
    from tests.support import MON, fixture

    path = root / "desk.json"
    desk = fixture(path)
    desk.change(settings.rule, "sound", "mix", "mon", "08:00", 60, MON)
    code, out, error = cli(path, "calendar", "sync", MON, MON, "--at", MON + "T06:00Z")
    assert code == 0, error
    assert "UID:sound:2024-05-06@crewcall\r\n" in out
    assert "SEQUENCE:0\r\n" in out


def main() -> None:
    subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
        check=True, timeout=12,
    )
    print("Legacy regression suite passed.", flush=True)
    with TemporaryDirectory(prefix="expert-verify-") as directory:
        smoke(Path(directory))


if __name__ == "__main__":
    main()
