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

    from crewcall import assignments, settings
    from tests.support import MON, TUE, fixture

    path = root / "desk.json"
    desk = fixture(path)
    desk.change(settings.rule, "night", "doors", "mon", "23:30", 90, MON)
    desk.change(assignments.add, f"night:{MON}", "ada")
    code, out, err = cli(path, "payroll", "report", MON, TUE)
    assert code == 0, err
    assert [(row["day"], row["minutes"]) for row in json.loads(out)] == [(MON, 30), (TUE, 60)]


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
