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
    "docs/adr/009-current.md": "f938b760611859824fc9e6f313b413196853bf7eb68bfb4509b5de7ac9687bb0",
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
    "verify.py": "2553ad0ab116f59e0fff4cfed8f42424dd804aac09f82882983bdf77c5278cef",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from quayside.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--ledger", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import csv
    import json
    from io import StringIO

    from quayside import invoices, payments, rates, statements
    from quayside.ledger import Ledger
    from quayside.model import Line
    from tests.support import FIRST, LAST, MID, fixture

    path = root / "ledger.json"
    ledger = fixture(path)
    ledger.change(invoices.issue, "west", 'signed, ticket', FIRST, [
        Line("tag", 1, "0.05"), Line("label", 1, "0.05"),
        Line("stickers", 3, "0.01"), Line("envelope", 1, "0.01"),
    ])
    ledger.change(invoices.issue, "west", "later", MID, [Line("Tag", 1, "0.05")])
    ledger.change(invoices.issue, "kw", "quiet", LAST, [Line("other", 1, "0.001")])
    ledger.change(payments.add, "west", FIRST, "0.05", "settled")
    ledger.change(payments.add, "west", MID, "-0.10", "refund")
    for target, day, value in (("EUR", FIRST, "0.5"), ("EUR", MID, "0.25"),
                               ("JPY", FIRST, "50"), ("KWD", FIRST, "0.05")):
        ledger.change(rates.put, "USD", target, day, value)

    args = ("statement", "open", FIRST, MID, "--currency", "eur")
    code, data, err = cli(path, *args)
    check((code, err), (0, ""))
    document = json.loads(data)
    check([row["amount"] for row in document["entries"]], ["0.06", "-0.02", "0.01", "0.02"])
    check(document["totals"], {"EUR": "0.07"})
    code, table, err = cli(path, *args, "--format", "csv")
    check((code, err), (0, ""))
    check(list(csv.DictReader(StringIO(table, newline=""))),
          [{key: str(value) for key, value in row.items()} for row in document["entries"]])
    check(json.loads(cli(path, "rate", "quote", "USD", "EUR", FIRST, "0.05")[1])["amount"], "0.03")
    state = Ledger(path).state
    check(statements.project(state, FIRST, FIRST, "JPY").totals, {"JPY": "4"})
    check([e.amount for e in statements.project(state, FIRST, FIRST, "KWD").entries],
          ["0.006", "-0.002"])
    check([e.amount for e in statements.project(state, FIRST, FIRST).entries], ["0.14", "-0.05"])
    check([e.amount for e in statements.project(state, FIRST, FIRST, "USD").entries], ["0.14", "-0.05"])
    check(cli(path, "statement", "open", FIRST, LAST, "--currency", "EUR"), (
        2, "", f"error: missing rate KWD/EUR for {LAST}\n",
    ))

    ledger.change(payments.add, "west", FIRST, "0.01", "zero in reporting")
    western = statements.project(Ledger(path).state, FIRST, FIRST, "JPY", "west")
    check(western.entries[-1].amount, "0")


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
