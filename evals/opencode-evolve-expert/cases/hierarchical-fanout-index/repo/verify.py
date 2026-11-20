"""Existing regressions and the issue's local reproduction."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def cli(home: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from harborrelay.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--home", str(home), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def smoke(root: Path) -> None:
    import json

    code, _, err = cli(root, "register", "regional", "depot.**")
    assert code == 0, err
    code, out, err = cli(root, "routes", "depot.west.in")
    assert code == 0, err
    assert json.loads(out) == ["regional"]


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
