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

    from quayside import invoices, rates
    from quayside.model import Line
    from tests.support import FIRST, fixture

    path = root / "ledger.json"
    ledger = fixture(path)
    ledger.change(invoices.issue, "west", "first", FIRST, [Line("stamp", 1, "0.05")])
    ledger.change(rates.put, "USD", "EUR", FIRST, "0.5")
    code, out, err = cli(path, "statement", "open", FIRST, FIRST, "--currency", "EUR")
    assert code == 0, err
    assert json.loads(out)["totals"] == {"EUR": "0.02"}


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
