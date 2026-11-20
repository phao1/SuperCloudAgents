"""Run the existing test suite and a happy-path check for supplier agreement renewals."""

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from groveboard.api import request

FIRST = {'id': 'summer-a',
 'agreement': 'spring-a',
 'day': '2025-05-01',
 'end': '2025-06-12',
 'cents': 395}
SECOND = {'id': 'summer-a', 'supplier': 'orchard', 'day': '2025-04-25'}


def smoke(root: Path) -> None:
    path = root / "workspace.json"
    copyfile("fixtures/workspace-v1.json", path)
    before = request(path, "clerk", "POST", "/v1/agreement-rollovers/draft", FIRST)
    assert before.status == 200, before.body
    assert before.body["data"]["status"] == 'draft'
    after = request(path, 'lead', "POST", "/v1/agreement-rollovers/activate", SECOND)
    assert after.status == 200, after.body
    assert after.body["data"]["status"] == 'active'


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
