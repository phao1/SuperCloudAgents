def smoke(path):
    from io import StringIO

    from stockroom.cli import run
    from stockroom.db import store
    from tests.support import seed

    with store(path) as connection:
        seed(connection)
    out, err = StringIO(), StringIO()
    code = run(["--db", str(path), "lot", "receive", "AX", "NORTH", "L4", "3", "2024-02-29",
                "--expires-on", "2024-03-01"], out, err)
    assert code == 0, err.getvalue()
    assert '"expires_on":"2024-03-01"' in out.getvalue()


import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
                   check=True, timeout=10)
    print("Legacy regression suite passed.", flush=True)
    with TemporaryDirectory(prefix="stockroom-verify-") as scratch:
        smoke(Path(scratch) / "warehouse.sqlite")


if __name__ == "__main__":
    main()
