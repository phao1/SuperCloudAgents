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
    "docs/adr/020-current.md": "f0aeab82e4ef319e33dd1f088aba89f8334bb55d6f9f1dcbd5693a6dc1f1fae5",
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
    "verify.py": "81021706b447c16e6bc6653de7c553ea7985adf768757824eda492ed0ffc44df",
}


def cli(home: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from harborrelay.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--home", str(home), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    from time import process_time

    from harborrelay.directory import Directory
    from harborrelay.dispatch import replenish
    from harborrelay.errors import RelayError
    from harborrelay.journal import Journal
    from harborrelay.leases import Worklist
    from harborrelay.routing import RoutingTable
    from harborrelay.types import Route
    from harborrelay.watch import Watch
    from tests.support import notice

    directory = Directory(root)
    directory.register("First", ("depot.**", "depot.west.in"), ("depot.west.private.**",))
    directory.register("All", ("**",), ("depot.*",))
    directory.register("Exact", ("depot.west",))
    directory.register("Rim", ("*.west.**",))
    restored = Directory(root)
    table = RoutingTable(restored.list())
    for stream, want in (
        ("depot", ["First", "All"]), ("depot.west", ["First", "Exact", "Rim"]),
        ("depot.west.in", ["First", "All", "Rim"]),
        ("depot.west.private", ["All", "Rim"]),
        ("depot.west.private.entry", ["All", "Rim"]),
        ("Depot.west", ["All", "Rim"]),
    ):
        check(table.targets(stream), want)
    for bad in ("**.zone", "zone.**.here", "*.***", ".**", "zone.**tail"):
        check(str(raises(RelayError, directory.register, "Valid", ("**",), (bad,))), "invalid pattern")

    restored.register("First", ("north.**",))
    check(table.targets("depot"), ["First", "All"])
    check(RoutingTable(restored.list()).targets("depot"), ["All"])
    journal = Journal(root)
    journal.append_many([notice("depot"), notice("depot.west.private"), notice("depot.west.in")])
    original = Directory(root)
    original.register("First", ("depot.**",), ("depot.west.private.**",))
    check([row.seq for row in Watch(journal, original).read("First").rows], [1, 3])
    worklist = Worklist(lambda: 0)
    check(replenish(worklist, journal, original, "First"), 2)
    check(worklist.pending(), [1, 3])

    size = 24_000
    rules = [Route(f"R{i}", (f"bay{i}.**",), (f"bay{i}.hold.**",)) for i in range(size)]
    rules.extend((Route("Common", ("*.ready",)), Route("Other", ("miss.**",))))
    start = process_time()
    table = RoutingTable(rules)
    for i in range(4_000):
        identity = (97 * i) % size
        if i % 5 == 0:
            query, expected = f"bay{identity}.hold", []
        elif i % 5 == 1:
            query, expected = f"unused{i}.ready", ["Common"]
        else:
            query, expected = f"bay{identity}.ready", [f"R{identity}", "Common"]
        check(table.targets(query), expected)
        if i % 32 == 0:
            assert process_time() - start < 4.0, "sparse fanout exceeded the CPU budget"


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
