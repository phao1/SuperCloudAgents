"""Run the existing test suite and a happy-path check for capacity-aware workshop windows."""

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from workyard.api import request

FIRST = {'id': 'tune-up',
 'tool': 'driver',
 'day': '2025-04-11',
 'end': '2025-04-14',
 'units': 4,
 'reason': 'inspection'}
SECOND = {'id': 'tune-up', 'day': '2025-04-13', 'outcome': 'quarantined'}


def smoke(root: Path) -> None:
    path = root / "workspace.json"
    copyfile("fixtures/workspace-v1.json", path)
    before = request(path, "clerk", "POST", "/v1/maintenance-windows/schedule", FIRST)
    assert before.status == 200, before.body
    assert before.body["data"]["status"] == 'scheduled'
    after = request(path, 'lead', "POST", "/v1/maintenance-windows/complete", SECOND)
    assert after.status == 200, after.body
    assert after.body["data"]["status"] == 'completed'


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
