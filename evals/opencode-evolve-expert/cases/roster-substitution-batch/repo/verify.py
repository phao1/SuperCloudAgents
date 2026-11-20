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
    from tests.support import MON, fixture

    path, sheet = root / "desk.json", root / "substitutes.json"
    desk = fixture(path)
    desk.change(settings.rule, "front", "doors", "mon", "09:00", 60, MON)
    shift = f"front:{MON}"
    desk.change(assignments.add, shift, "ada")
    rows = [{"shift": shift, "from": "ada", "to": "lin"}]
    sheet.write_text(json.dumps(rows))
    code, out, error = cli(path, "roster", "substitute", str(sheet))
    assert code == 0, error
    assert json.loads(out) == rows
    assert json.loads(cli(path, "roster", "crew", shift)[1]) == ["lin"]


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
