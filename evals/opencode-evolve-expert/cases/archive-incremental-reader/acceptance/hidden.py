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
    "docs/adr/020-current.md": "b290b52265788e29c2e84fc861b4bb42600fd973aa0ceef9f7e68ee0d785275b",
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
    "verify.py": "e0f2700ceeaaa12bd128ba228725c759500945ab3684ee2944c1472b6f58b581",
}


def feature(root: Path) -> None:
    from random import Random
    from struct import pack

    from harborrelay.errors import ArchiveError
    from harborrelay.exchange import load
    from harborrelay.journal import Journal
    from harborrelay.wire import MAGIC, MAX_BODY, encode_pack, iter_frames
    from tests.support import event, notice

    rows = [event(6, memo="端口\nCafé"), event(5, boxes=[0, "🐡"]), event(7)]
    archive = encode_pack(rows)
    rng = Random(107)
    for _ in range(40):
        chunks = [b""]
        start = 0
        while start < len(archive):
            end = start + rng.randint(1, 31)
            chunks.extend((archive[start:end], b""))
            start = end
        try:
            check(list(iter_frames(iter(chunks))), rows)
        except ArchiveError as error:
            raise AssertionError("a valid archive with empty chunks was rejected") from error

    error = RuntimeError("reader disconnected")
    first = encode_pack([rows[0]])

    def source():
        yield first[:5]
        yield first[5:]
        raise error

    eager = iter_frames(source())
    try:
        check(next(eager), rows[0])
    except RuntimeError as caught:
        raise AssertionError("a complete frame caused a lookahead read") from caught
    assert raises(RuntimeError, next, eager) is error

    for invalid_length in (0, MAX_BODY + 1):
        def oversized(length: int):
            yield MAGIC + pack(">I", length)
            raise AssertionError("invalid length requested more data")

        check(str(raises(ArchiveError, next, iter_frames(oversized(invalid_length)))), "invalid archive length")

    # A trailing partial frame is rejected at consumption, not while yielding the prior row.
    for tail in (b"\0", pack(">I", 3), pack(">I", 1) + b"x" + b"\0\0\0"):
        iterated = iter_frames([first + tail])
        check(next(iterated), rows[0])
        check(str(raises(ArchiveError, next, iterated)), "truncated archive frame")
    with_checksum = first + pack(">I", 1) + b"{" + bytes(4)
    iterated = iter_frames([with_checksum])
    check(next(iterated), rows[0])
    check(str(raises(ArchiveError, next, iterated)), "archive checksum mismatch")

    journal = Journal(root)
    before = journal.append(notice())
    corrupt = root / "archive.hr"
    corrupt.write_bytes(with_checksum)
    check(str(raises(ArchiveError, load, journal, corrupt)), "archive checksum mismatch")
    check(Journal(root).all(), [before])


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
