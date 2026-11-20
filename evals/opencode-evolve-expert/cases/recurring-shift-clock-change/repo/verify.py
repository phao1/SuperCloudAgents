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
    import json

    from crewcall import settings
    from tests.support import fixture

    path = root / "desk.json"
    desk = fixture(path)
    day = "2024-03-31"
    desk.change(settings.rule, "sunday", "mix", "sun", "02:20", 90, day)
    code, out, err = cli(path, "roster", "show", day, day)
    assert code == 0, err
    shift, = json.loads(out)
    assert (shift["start"], shift["end"]) == ("2024-03-31T01:00:00Z", "2024-03-31T02:30:00Z")


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
