"""Independent acceptance checks for this fixture."""

import signal
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory


def check(actual, expected) -> None:
    assert type(actual) is type(expected) and actual == expected, (actual, expected)


def raises(error_type, call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except error_type as error:
        return error
    raise AssertionError(f"{call!r} did not raise {error_type.__name__}")


PROTECTED = {
    "README.md": "8c1bc36494e95d1f443093c56ad60cf3fcc8d5e678cb2bc7a9bf289cdf2be02e",
    "docs/adr/020-current.md": "beb9f55a0e8ed12c7eb6e96a4c404735a18e54c623c45d3850e7554a92847a84",
    "docs/exchange.md": "485dd457d6f46caf4eeee71138078602b9e077dd55428bf4741a3a23d5504f20",
    "docs/journal.md": "9cf4ca14d67fe1be45c7e8c2f744a43647c933fd12964947f216509fd29b42ed",
    "docs/operator.md": "d60bd3c0f43438f6c0606bf627a2f780d84cb268095e71ece920ec737ecaa6c7",
    "docs/workers.md": "d0aa1b5170d8605d6b0acbd9a81b1776a7a580e10ab52103708926625a44de3d",
    "tests/__init__.py": "43d7a487284ca21cfbb71f588ffda6392b0eb1ca88e6d1e5348ced4a1cd9940b",
    "tests/support.py": "a1700d0f801fde7c9da75a4025b8c8c0117cf59362615a379e8f286dfa5349e4",
    "tests/test_audit.py": "4a891dea1a8e88b9ae5bf65044e3c62da8e2a6a83d90efa6b8e8a6882081d659",
    "tests/test_cli.py": "f18079cd3db1a4177b8e601f518c0d27a6bb7e81e35692a871785ffe3527e325",
    "tests/test_encoding.py": "6f6bc770350d96c185e334095ebc385f3e57804a21824f1bcadf82de82890557",
    "tests/test_journal.py": "1c0a4f7e5c44b52c3b548d8a9844d3c0bdbbf6ce6d0e5b5653e4503ecc1cf5d7",
    "tests/test_packs.py": "74546d485a45cd9ef11e34a3ad77a78d64c5971e8992111427c50df5e48d3b97",
    "tests/test_routes.py": "9314e8d70e24de5c16d92ad0364c37d4ed59e085944e61a3cfa8b5c13e5fb4fc",
    "tests/test_worklist.py": "6105faccee741701c87e7e513f4979ef82ee1b1362273467cb07443203c416bc",
    "verify.py": "beee74822ef0bb3278a733ffaa67b90bdb4eb3f7337504a8ad97c2e52465ff12",
}


def cli(home: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from harborrelay.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--home", str(home), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json
    from unittest.mock import patch

    from harborrelay.encoding import row_bytes
    from harborrelay.errors import RelayError
    from harborrelay.journal import Journal
    from tests.support import event, notice

    path = root / "events.ndjson"
    path.write_bytes(b"\n".join(map(row_bytes, [event(3), event(5), event(8)])) + b"\n")
    journal = Journal(root)
    check(journal.head, 8)
    old = path.read_bytes()
    check(journal.prune(2), 0)
    check(path.read_bytes(), old)
    check(journal.prune(6), 2)
    check(journal.read_after(5), [event(8)])
    check(str(raises(RelayError, journal.read_after, 4)), "history starts after 5")
    check(cli(root, "journal", "--after", "4"), (2, "", "error: history starts after 5\n"))
    code, out, error = cli(root, "journal", "--limit", "1")
    check((code, error), (0, ""))
    check([row["seq"] for row in json.loads(out)], [8])
    check(cli(root, "archive", "export", str(root / "export.hr"))[0], 0)
    check(cli(root, "register", "routes", "dock.*")[0], 0)
    check(cli(root, "watch", "routes", "--after", "4"), (2, "", "error: history starts after 5\n"))

    before_failure = path.read_bytes()
    with patch("harborrelay.journal.replace_bytes", side_effect=RelayError("write fault")):
        check(str(raises(RelayError, journal.prune, 900)), "write fault")
    check(journal.read_after(5), [event(8)])
    check(path.read_bytes(), before_failure)

    check(journal.prune(900), 1)
    check(journal.head, 8)
    reopened = Journal(root)
    check(reopened.head, 8)
    check(reopened.all(), [])
    check(reopened.read_after(8), [])
    check(str(raises(RelayError, reopened.read_after, 7)), "history starts after 8")
    check([row.seq for row in reopened.append_many([notice(), notice()])], [9, 10])
    check(Journal(root).head, 10)
    check(Journal(root).prune(9), 1)
    check([row.seq for row in Journal(root).read_after(9)], [10])
    for raw in (b'{"pruned_through":true}\n', b'{"pruned_through":3}\n' + row_bytes(event(3))):
        path.write_bytes(raw)
        check(str(raises(RelayError, Journal, root)), "invalid journal file")
        check(path.read_bytes(), raw)


def main() -> int:
    signal.setitimer(signal.ITIMER_REAL, 24)
    for name, digest in PROTECTED.items():
        path = Path(name)
        assert path.is_file() and not path.is_symlink(), name
        assert sha256(path.read_bytes()).hexdigest() == digest, name
    public = subprocess.run([sys.executable, "-B", "verify.py"], check=False, timeout=14)
    if public.returncode != 0:
        print("Public verification failed.", flush=True)
        return 1
    print("Public verification passed.", flush=True)
    try:
        with TemporaryDirectory(prefix="expert-acceptance-") as directory:
            feature(Path(directory))
    except AssertionError:
        print("Feature assertion failed.", flush=True)
        raise
    print("Feature checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
