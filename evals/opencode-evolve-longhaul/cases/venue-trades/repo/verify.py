"""Run the existing test suite and a happy-path check for venue block trades."""

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from framehouse.api import request

FIRST = {'id': 'room-switch', 'left': 'block-b', 'right': 'block-a', 'day': '2025-04-10'}
SECOND = {'id': 'room-switch', 'day': '2025-04-11'}


def smoke(root: Path) -> None:
    path = root / "workspace.json"
    copyfile("fixtures/workspace-v1.json", path)
    before = request(path, "clerk", "POST", "/v1/venue-trades/propose", FIRST)
    assert before.status == 200, before.body
    assert before.body["data"]["status"] == 'proposed'
    after = request(path, 'lead', "POST", "/v1/venue-trades/approve", SECOND)
    assert after.status == 200, after.body
    assert after.body["data"]["status"] == 'approved'


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
