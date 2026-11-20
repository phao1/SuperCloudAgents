"""Run the existing test suite and a happy-path check for delivery shortfall credits."""

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from groveboard.api import request

FIRST = {'id': 'credit-a',
 'line': 'drop-a',
 'units': 5,
 'day': '2025-04-11',
 'reason': 'Cases missing'}
SECOND = {'id': 'credit-a', 'granted': 4, 'day': '2025-04-12'}


def smoke(root: Path) -> None:
    path = root / "workspace.json"
    copyfile("fixtures/workspace-v1.json", path)
    before = request(path, "clerk", "POST", "/v1/shortfall-credits/claim", FIRST)
    assert before.status == 200, before.body
    assert before.body["data"]["status"] == 'claimed'
    after = request(path, 'lead', "POST", "/v1/shortfall-credits/issue", SECOND)
    assert after.status == 200, after.body
    assert after.body["data"]["status"] == 'issued'


def main() -> None:
    subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
        check=True, timeout=8,
    )
    print("Legacy regression suite passed.", flush=True)
    with TemporaryDirectory(prefix="longhaul-verify-") as directory:
        smoke(Path(directory))


if __name__ == "__main__":
    main()
