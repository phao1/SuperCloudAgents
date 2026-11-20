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

    db, order = root / "depot.sqlite", root / "order.json"
    seed_file(db)
    cli(db, "lot", "receive", "ax", "north", "L", "8", "2024-05-01")
    order.write_text('[{"sku":"ax","packs":2,"size":3}]')
    code, out, err = cli(db, "order", "pack", str(order))
    assert code == 0, err
    assert [(row["line"], row["quantity"]) for row in json.loads(out)["picks"]] == [(1, 6)]


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
