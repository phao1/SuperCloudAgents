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
    "docs/adr/009-current.md": "482424a4d77c3e7f0cd5237bbb305a536d7cb4b3b04c9876506586bf7eaf33ba",
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
    "verify.py": "e780a904924aa2265c91dd49d29977d4c1f742e763a5fdbc5419ceaf04ddbe4e",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from quayside.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--ledger", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json

    from quayside import invoices, payments, rates
    from quayside.ledger import Ledger
    from quayside.model import Line
    from tests.support import FIRST, LAST, MID, fixture

    path = root / "ledger.json"
    ledger = fixture(path)
    source = ledger.change(invoices.issue, "kw", "Crates", FIRST, [
        Line("Zulu", 1, "0.001"), Line("Alpha", 1, "0.001"),
        Line("Bravo", 1, "0.001"), Line("Free", 5, "0"),
    ])

    def submit(day: str, amount: str, reference: str, invoice="1") -> dict:
        code, out, error = cli(path, "invoice", "credit", invoice, day, amount, reference)
        check((code, error), (0, ""))
        return json.loads(out)

    first = submit(MID, "0.0010", "Note, one ")
    check(first, {"id": 1, "invoice": 1, "day": MID, "reference": "Note, one ",
                  "amount": "0.001", "lines": [{"line": 1, "amount": "0.001"}]})
    second = submit(LAST, "0.001", "note two")
    check(second["lines"], [{"line": 2, "amount": "0.001"}])
    check(json.loads(cli(path, "invoice", "credits", "1")[1]), [first, second])
    stable = path.read_bytes()
    for date, value, message in [
        (LAST, "0.002", "credit exceeds remaining invoice amount"),
        (LAST, "0.000", "credit must be positive"), (LAST, "-0.001", "credit must be positive"),
        ("2024-06-30", "0.001", "credit precedes invoice"),
        (LAST, "0.0001", "amount exceeds currency precision"),
    ]:
        check(cli(path, "invoice", "credit", "1", date, value, "bad"),
              (2, "", f"error: {message}\n"))
        check(path.read_bytes(), stable)
    check(invoices.get(Ledger(path).state, 1), source)

    def debt(day: str) -> str:
        report = json.loads(cli(path, "statement", "aging", day, "--account", "kw")[1])
        return report["rows"][0]["remaining"]

    check(debt(FIRST), "0.003")
    check(debt(MID), "0.002")
    check(debt(LAST), "0.001")
    ledger = Ledger(path)
    ledger.change(payments.add, "kw", LAST, "0.001", "collected")
    ledger.change(rates.put, "KWD", "USD", MID, "10")
    aging = json.loads(cli(path, "statement", "aging", LAST, "--account", "kw")[1])
    check(aging["rows"], [])
    report = json.loads(cli(path, "statement", "open", MID, LAST, "--currency", "USD")[1])
    check([(row["kind"], row["id"], row["amount"]) for row in report["entries"]],
          [("credit", 1, "-0.01"), ("credit", 2, "-0.01"), ("payment", 1, "-0.01")])
    check(report["totals"], {"USD": "-0.03"})

    ledger.change(invoices.issue, "west", "Spools", FIRST, [
        Line("C", 1, "1"), Line("B", 2, "0.50"), Line("A", 1, "1"),
    ])
    third = submit(LAST, "0.05", "allowance", invoice="2")
    check(third["id"], 3)
    check(third["lines"], [{"line": 1, "amount": "0.02"}, {"line": 2, "amount": "0.02"},
                           {"line": 3, "amount": "0.01"}])
    check(submit(LAST, "0.001", "last share")["lines"], [{"line": 3, "amount": "0.001"}])


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
