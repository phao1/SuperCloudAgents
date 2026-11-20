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
    from harborrelay.journal import Journal
    from tests.support import notice

    journal = Journal(root)
    assert journal.append(notice()).seq == 1
    assert journal.prune(1) == 1
    assert journal.append(notice()).seq == 2


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
