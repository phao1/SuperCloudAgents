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
    "README.md": "51d308ade63f80ce8b9f34dd05b4972b4d637c065dde508a09b7a6f61b838bce",
    "docs/adr/009-current.md": "f728618ae69f8853608ce28e9c2f72536e1e7a0aeb77ca79cf1da6d1a586d613",
    "docs/aging.md": "9b6f8eaa9bdc0bc3a35195b8bb3e7c79e739aace677b55b84c04afb8a3388261",
    "docs/bank-register.md": "d161484120bb2263a138cc3ac4fd17106622619e6afd5c053f37c05df8ee4a9e",
    "docs/exchange.md": "766ce08d84c27de169a4d6d3e89d78296e09f14affb5c8d6163c3e46996da5c6",
    "docs/posting.md": "fc65619dbf02f5509100938defeeaf7e34f4e5bc7144fa9eb9a66fa27dd7378f",
    "docs/statement.md": "0ccd6f4da522cebb3e48f48cbb7ed1f3e24a429d95a430a074e6f1c9bd46335a",
    "tests/__init__.py": "a623806ce6077049f3479586574fc757af7b29b7de0a08d4ed0a6b8bc2a2d5b3",
    "tests/support.py": "cd3fcf631ed3b142d442f2984dc23356b6be5b7b437b76e91897fc0c6468d3f6",
    "tests/test_aging.py": "e31ebfc88180c10fefb9072cf9f08f282aae25d4c2611aef961e0d114dabff3c",
    "tests/test_exchange.py": "aa5a7d61848736e409bee76ea77365fdaf315c85d405fd96dd4060d1b686b768",
    "tests/test_money.py": "d8758e15d6d71dfd058239971092ce561a3ad3f574c7a3b52533f9840f00c4b1",
    "tests/test_posts.py": "b1b926371039722a75283ad529f4f9c636f53c863a486466fa76ea5dbc09a93e",
    "tests/test_register.py": "671e4f7fcfd52ca840d0f16b7d729f2ef1debecd9dbc32c090f0619fe6914217",
    "tests/test_reports.py": "583520dd371a41695ea3f5d0c13b9cd4d9b9485a04b9aef274c9d376325b1c66",
    "tests/test_workflow.py": "1608d9b936892e4f8b75efc445ae0e945672b6710a145aa6192190c00377ae9e",
    "verify.py": "9b68e3b7928d4f3f1a355788714c30e049c87fce82d79f6bd229b7ced80bd0b8",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from quayside.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--ledger", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json

    from quayside import payments
    from quayside.ledger import Ledger
    from tests.support import FIRST, LAST, MID, fixture

    path, target, archive = root / "source.json", root / "sink.json", root / "wire.json"
    source = fixture(path)
    sink = fixture(target)
    sink.change(payments.add, "west", FIRST, "5", "local")
    for account, day, value, text in [
        ("kw", LAST, "1.125", "أيار"), ("yen", FIRST, "-12", "補正"), ("west", MID, "-0.05", "old, ref"),
    ]:
        source.change(payments.add, account, day, value, text)
    original = path.read_bytes()
    check(cli(path, "archive", "export", str(archive), "--version", "2"),
          (0, '{"exported":3}\n', ""))
    expected = [
        {"account": "KW", "day": LAST, "reference": "أيار", "currency": "KWD", "amount": "1.125"},
        {"account": "YEN", "day": FIRST, "reference": "補正", "currency": "JPY", "amount": "-12"},
        {"account": "WEST", "day": MID, "reference": "old, ref", "currency": "USD", "amount": "-0.05"},
    ]
    check(json.loads(archive.read_bytes()), {"version": 2, "payments": expected})
    check(path.read_bytes(), original)
    saved = archive.read_bytes()
    check(cli(path, "archive", "export", str(archive)),
          (2, "", "error: archive v1 requires two-place currency\n"))
    check(archive.read_bytes(), saved)
    code, out, error = cli(target, "archive", "import", str(archive))
    check((code, error), (0, ""))
    check([(row["id"], row["amount"], row["account"]) for row in json.loads(out)],
          [(2, "1.125", "KW"), (3, "-12", "YEN"), (4, "-0.05", "WEST")])

    stable = target.read_bytes()

    def test_row(row, version, message):
        archive.write_text(json.dumps({"version": version, "payments": [row]}, ensure_ascii=False))
        check(cli(target, "archive", "import", str(archive)), (2, "", f"error: {message}\n"))
        check(target.read_bytes(), stable)

    west = expected[2]
    for value in ("-0.050", "0.5", "-0.00", "+0.50", ".50", "1e0", "01.00", True, 0.1):
        test_row(west | {"amount": value}, 2, "invalid archive payment")
    test_row(west | {"currency": "usd"}, 2, "invalid archive payment")
    test_row(west | {"currency": "EUR"}, 2, "archive currency mismatch")
    test_row({"account": "yen", "day": MID, "reference": "legacy", "cents": 1200},
             1, "archive v1 requires two-place currency")
    archive.write_text(json.dumps({"version": 2, "payments": [
        west, expected[0] | {"currency": "EUR"},
    ]}, ensure_ascii=False))
    check(cli(target, "archive", "import", str(archive)),
          (2, "", "error: archive currency mismatch\n"))
    check(target.read_bytes(), stable)
    check(cli(path, "archive", "export", str(archive), "--account", "West")[0], 0)
    check(json.loads(archive.read_bytes()), {"version": 1, "payments": [{
        "account": "WEST", "day": MID, "reference": "old, ref", "cents": -5,
    }]})
    check(cli(target, "archive", "import", str(archive))[0], 0)
    check(Ledger(target).state.payments[-1].id, 5)


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
