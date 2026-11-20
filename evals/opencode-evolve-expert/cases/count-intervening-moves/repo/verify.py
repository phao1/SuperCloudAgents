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
    cli(path, "lot", "receive", "ax", "north", "A", "4", "2024-05-01")
    cli(path, "count", "open", "north", "audit", "2024-05-02")
    cli(path, "count", "record", "1", "1", "3")
    for delta in ["1", "-1"]:
        cli(path, "lot", "adjust", "ax", "north", "A", delta, "2024-05-03")
    assert cli(path, "count", "close", "1", "2024-05-03") == (
        2, "", "error: stock changed during count\n",
    )


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
