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

    from quayside import invoices
    from quayside.model import Line
    from tests.support import FIRST, fixture

    path = root / "ledger.json"
    ledger = fixture(path)
    invoice = ledger.change(invoices.issue, "west", "Café", FIRST, [Line("paper", 1, "1")])
    code, out, err = cli(path, "invoice", "find", "West", "Cafe\u0301")
    assert code == 0, err
    assert json.loads(out)["id"] == invoice.id


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
