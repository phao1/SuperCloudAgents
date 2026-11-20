"""Run the existing test suite and a happy-path check for batch sample holds and dispositions."""

import subprocess
import sys
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from watershed.api import request

FIRST = {'id': 'label-check',
 'samples': ['sample-b', 'sample-a'],
 'day': '2025-04-11',
 'reason': 'Vérifier les étiquettes'}
SECOND = {'id': 'label-check',
 'dispositions': {'sample-a': 'cleared', 'sample-b': 'discarded'},
 'day': '2025-04-12'}


def smoke(root: Path) -> None:
    path = root / "workspace.json"
    copyfile("fixtures/workspace-v1.json", path)
    before = request(path, "clerk", "POST", "/v1/sample-holds/place", FIRST)
    assert before.status == 200, before.body
    assert before.body["data"]["status"] == 'held'
    after = request(path, 'lead', "POST", "/v1/sample-holds/resolve", SECOND)
    assert after.status == 200, after.body
    assert after.body["data"]["status"] == 'resolved'


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
