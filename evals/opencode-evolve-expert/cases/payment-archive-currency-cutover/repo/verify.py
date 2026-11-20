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

    from quayside import payments
    from tests.support import FIRST, fixture

    path, archive = root / "ledger.json", root / "wire.json"
    ledger = fixture(path)
    ledger.change(payments.add, "west", FIRST, "0.35", "deposit")
    result = cli(path, "archive", "export", str(archive), "--version", "2")
    assert result[0] == 0, result
    output = json.loads(archive.read_text())
    assert output["version"] == 2 and output["payments"][0]["amount"] == "0.35"
    assert cli(path, "archive", "import", str(archive))[0] == 0


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
