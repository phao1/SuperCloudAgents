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
    "docs/adr/009-current.md": "d880f58d9a5bdc5bc269c13cecae269ea69a5ab89f12bd59c76d5d56ed9cd425",
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
    "verify.py": "fcbfdffa36a2ed84bd7b201e7afbb34b9f0ea12d152169409869da597a4be476",
}


def cli(path: Path, *arguments: str) -> tuple[int, str, str]:
    from io import StringIO

    from quayside.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--ledger", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()



def feature(root: Path) -> None:
    import json

    from quayside.ledger import Ledger
    from tests.support import FIRST, MID, fixture

    path, feed = root / "ledger.json", root / "feed.json"
    fixture(path)

    def apply(*rows):
        feed.write_text(json.dumps(rows, ensure_ascii=False))
        return cli(path, "payment", "bank-import", str(feed))

    reference = "入金, Dock "
    base = {"account": "west", "day": FIRST, "amount": "1.000", "reference": reference}
    posting = {**base, "bank": "River", "receipt": "US:7.1"}
    retry = {**posting, "account": "WEST", "bank": "RIVER", "amount": "1.00"}
    first = apply(posting, retry, base, posting)
    check((first[0], first[2]), (0, ""))
    result = json.loads(first[1])
    check([row["id"] for row in result], [1, 1, 2, 1])
    check(result[0], result[1])
    check(set(result[0]), {"id", "account", "day", "amount", "reference"})
    prior = path.read_bytes()
    check(apply(posting)[1], json.dumps(result[:1], ensure_ascii=False, separators=(",", ":")) + "\n")
    check(path.read_bytes(), prior)

    new = {**posting, "receipt": "new"}
    for wrong in ({"amount": "1.01"}, {"day": MID}, {"reference": reference.strip()}, {"account": "east"}):
        check(apply(new, {**posting, **wrong}), (2, "", "error: bank receipt already used\n"))
        check(path.read_bytes(), prior)
    second = apply(new, {**posting, "bank": "Other"}, {**posting, "receipt": "us:7.1"}, base)
    check((second[0], second[2]), (0, ""))
    check([row["id"] for row in json.loads(second[1])], [3, 4, 5, 6])
    check([row.id for row in Ledger(path).state.payments], [1, 2, 3, 4, 5, 6])
    prior = path.read_bytes()
    for bad in ({"bank": ""}, {"receipt": " id"}, {"receipt": None}, {"receipt": "x" * 65},
                {"receipt": "é"}, {"receipt": False}):
        check(apply({**posting, **bad}), (2, "", "error: invalid bank receipt\n"))
        check(path.read_bytes(), prior)
    check(apply({**base, "bank": "L"}), (2, "", "error: invalid remittance rows\n"))
    check(apply(), (0, "[]\n", ""))


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
