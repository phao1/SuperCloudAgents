"""Existing regressions and the issue's local reproduction."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def cli(path: Path, *args: str) -> tuple[int, str, str]:
    from io import StringIO

    from stockroom.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--db", str(path), *args], out, err)
    return code, out.getvalue(), err.getvalue()


def seed_file(path: Path) -> None:
    from stockroom.db import store
    from tests.support import seed

    with store(path) as connection:
        seed(connection)



def smoke(root: Path) -> None:
    import json

    path = root / "depot.sqlite"
    seed_file(path)
    for code, day in [("A", "2024-05-01"), ("B", "2024-05-02")]:
        assert cli(path, "lot", "receive", "ax", "north", code, "2", day)[0] == 0
    args = ("report", "activity-page", "--size", "1")
    code, out, err = cli(path, *args)
    assert code == 0, err
    page = json.loads(out)
    assert [row["lot"] for row in page["rows"]] == ["A"] and page["next"]
    code, out, err = cli(path, *args, "--cursor", page["next"])
    assert code == 0, err
    assert json.loads(out)["next"] is None
    assert [row["lot"] for row in json.loads(out)["rows"]] == ["B"]


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
