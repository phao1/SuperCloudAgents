"""Existing regressions and the issue's local reproduction."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def smoke(root: Path) -> None:
    from harborrelay.wire import encode_pack, iter_frames
    from tests.support import event

    chunks = [encode_pack([event(4)]), encode_pack([event(9)])[4:]]
    pulls = []

    def source():
        for index, piece in enumerate(chunks):
            pulls.append(index)
            yield piece
        pulls.append("end")

    rows = iter_frames(source())
    assert not pulls
    assert next(rows) == event(4)
    assert "end" not in pulls


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
