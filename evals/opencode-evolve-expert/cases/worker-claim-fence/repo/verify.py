"""Existing regressions and the issue's local reproduction."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def smoke(root: Path) -> None:
    from harborrelay.errors import RelayError
    from harborrelay.leases import Worklist
    from tests.support import event

    time = [10]
    queue = Worklist(lambda: time[0])
    queue.enqueue(event(1))
    original = queue.claim("Ada", 5)
    time[0] = 15
    current = queue.claim("Ada", 5)
    try:
        queue.ack(original.lease)
    except RelayError as error:
        assert str(error) == "stale lease"
    else:
        raise AssertionError("stale worker acknowledged the replacement")
    queue.ack(current.lease)


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
