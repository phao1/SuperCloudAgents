def smoke(path):
    from io import StringIO

    from stockroom.cli import run
    from stockroom.db import store
    from tests.support import seed

    with store(path) as connection:
        seed(connection)
    out, err = StringIO(), StringIO()
    code = run(["--db", str(path), "item", "rename", "AX", "AX2"], out, err)
    assert code == 0, err.getvalue()
    assert '"sku":"AX2"' in out.getvalue()


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
