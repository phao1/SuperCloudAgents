"""Existing regressions and the issue's local reproduction."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from quayside.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--ledger", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def smoke(root: Path) -> None:
    import json

    from tests.support import FIRST, fixture

    path, feed = root / "ledger.json", root / "feed.json"
    fixture(path)
    feed.write_text(json.dumps([{
        "account": "west", "day": FIRST, "amount": "2", "reference": "Deposit",
        "bank": "bank1", "receipt": "X-3",
    }]))
    first = cli(path, "payment", "bank-import", str(feed))
    assert first[0] == 0, first
    assert cli(path, "payment", "bank-import", str(feed)) == first


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
