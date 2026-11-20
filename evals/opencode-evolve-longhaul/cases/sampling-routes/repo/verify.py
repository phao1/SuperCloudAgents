"""Run the existing test suite and a happy-path check for multi-station sampling routes."""

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from watershed.api import request

FIRST = {'id': 'brook-loop',
 'crew': 'brook',
 'day': '2025-04-10',
 'visits': ['visit-c', 'visit-b', 'visit-a']}
SECOND = {'id': 'brook-loop', 'day': '2025-04-09'}


def smoke(root: Path) -> None:
    path = root / "workspace.json"
    copyfile("fixtures/workspace-v1.json", path)
    before = request(path, "clerk", "POST", "/v1/sampling-routes/plan", FIRST)
    assert before.status == 200, before.body
    assert before.body["data"]["status"] == 'planned'
    after = request(path, 'lead', "POST", "/v1/sampling-routes/dispatch", SECOND)
    assert after.status == 200, after.body
    assert after.body["data"]["status"] == 'dispatched'


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
