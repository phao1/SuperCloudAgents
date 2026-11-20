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
    "docs/adr/009-current.md": "bea5fe9bc6cf95063a2ec152ea2fd8d395c732706662b8922bf5d225db875ad3",
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
    "verify.py": "db3339d152962adbb93c37d9054bcc0f9372b806f05075056b942eba399d5349",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from quayside.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--ledger", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json
    from dataclasses import replace

    from quayside import invoices, lookup, storage
    from quayside.errors import LedgerError
    from quayside.ledger import Ledger
    from quayside.model import Line
    from tests.support import FIRST, fixture

    path = root / "ledger.json"
    ledger = fixture(path)
    lines = [Line("Carrier", 1, "1")]
    printed = "\u2003Café Straße Σ\u00a0"
    western = ledger.change(invoices.issue, "west", printed, FIRST, lines)
    eastern = ledger.change(invoices.issue, "east", "cafe\u0301 STRASSE ς", FIRST, lines)
    forms = ("CAFE\u0301 STRASSE σ", "café strasse ς", "\u2009Café STRAẞE Σ ")
    for text in forms:
        code, data, err = cli(path, "invoice", "find", "west", text)
        check((code, err), (0, ""))
        check((json.loads(data)["id"], json.loads(data)["reference"]), (western.id, printed))
        check(lookup.find(Ledger(path).state, "east", text), eastern)

    original = path.read_bytes()
    source = root / "invoice.json"
    source.write_text('[{"description":"New","units":1,"unit_price":"2"}]')
    check(cli(path, "invoice", "issue", "west", forms[0], FIRST, str(source)),
          (2, "", "error: invoice reference already exists\n"))
    check(path.read_bytes(), original)
    pairs = [("A-11", "Ａ-11"), ("north hall", "north  hall")]
    for left, right in pairs:
        old = ledger.change(invoices.issue, "west", left, FIRST, lines)
        new = ledger.change(invoices.issue, "west", right, FIRST, lines)
        check(new.id, old.id + 1)
        check(lookup.find(Ledger(path).state, "west", right), new)

    # Older software could create this collision even though the dates and records are valid.
    collision = replace(western, id=999, reference="CAFÉ STRASSE Σ")
    storage.write(path, replace(ledger.state, invoices=(*ledger.state.invoices, collision)))
    snapshot = path.read_bytes()
    check(cli(path, "invoice", "find", "west", collision.reference),
          (2, "", "error: ambiguous invoice reference\n"))
    state = Ledger(path).state
    check(str(raises(LedgerError, lookup.find, state, "west", printed)), "ambiguous invoice reference")
    check(invoices.get(state, western.id), western)
    check(invoices.get(state, collision.id), collision)
    check(path.read_bytes(), snapshot)
    code, out, err = cli(path, "invoice", "issue", "west", "unrelated", FIRST, str(source))
    check((code, err), (0, ""))
    check(json.loads(out)["id"], 1000)


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
