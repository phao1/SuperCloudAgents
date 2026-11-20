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
    path = root / "depot.sqlite"
    seed_file(path)
    assert cli(path, "lot", "receive", "ax", "north", "A", "8", "2024-05-01")[0] == 0
    args = ("lot", "move", "ax", "north", "south", "3", "2024-05-02", "--request-id", "M1")
    first = cli(path, *args)
    assert first[0] == 0, first
    assert cli(path, *args) == first


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
