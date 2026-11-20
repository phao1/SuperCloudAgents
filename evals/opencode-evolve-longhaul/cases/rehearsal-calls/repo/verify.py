"""Run the existing test suite and a happy-path check for published rehearsal calls."""

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from framehouse.api import request

FIRST = {'id': 'read-through',
 'scope': 'north',
 'production': 'blue',
 'day': '2025-04-14',
 'crew': ['bilal', 'ana']}
SECOND = {'id': 'read-through', 'day': '2025-04-12', 'expected_count': 2}


def smoke(root: Path) -> None:
    path = root / "workspace.json"
    copyfile("fixtures/workspace-v1.json", path)
    before = request(path, "clerk", "POST", "/v1/rehearsal-calls/draft", FIRST)
    assert before.status == 200, before.body
    assert before.body["data"]["status"] == 'draft'
    after = request(path, 'lead', "POST", "/v1/rehearsal-calls/publish", SECOND)
    assert after.status == 200, after.body
    assert after.body["data"]["status"] == 'published'


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
