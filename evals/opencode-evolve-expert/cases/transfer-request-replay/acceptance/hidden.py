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
    "README.md": "6aadd718a6da4428027e4364acc5b75aa6a0f20721ea2da6c7889eceb675cfbe",
    "docs/adr/018-release.md": "8e489410ad70582e6657d5ebee8405476066e2f0e0b6746e50d371d2d59d9a82",
    "docs/commands.md": "3d3cc1837adbc85976e4ea1d56f80a4c857ce9b549c8203020cf83fea3327f3b",
    "docs/feature.md": "b0817df67ea79d3a05aa5088c943867bda63b15ce48fc0e7342eeb3501a43a4b",
    "docs/storage.md": "205b429eb4d9166d2e1396daa669a1a6ad82f6f0c403fcf0d36b9648d3014631",
    "stockroom/schema.sql": "5a79ca26adca26d15286ed5d66f421c0e03959b0205783e93533671fdb2faae0",
    "tests/__init__.py": "37c6c32e6a930b7f0021d7617b46c203dc2af08e3a7c8950c98848a3792d0e6c",
    "tests/support.py": "204a88bcda54d60c461c62f7ec7183495388958581ccbbd6f766998f4afc838a",
    "tests/test_catalog.py": "5c48c58f034004b2645b4f312c774872661d362163f55ded6a332c373059202c",
    "tests/test_cli.py": "b01b6a82c7a1541f8c7e57b970da60b5dd6eae9329504cf709429d09816c658e",
    "tests/test_database.py": "0d80193412c0c1f58cbf18d1fb1f14907fa0cbaf45af9655426964cd29286e1f",
    "tests/test_files.py": "b60decb00d8c80ead887c968101853bf7742126169b4eaab2f221be4270a463f",
    "tests/test_fulfillment.py": "daaec93ff314107fef7b5f13007c74bddd7c57ade34662df1ab10dc02a7575e3",
    "tests/test_history.py": "7e712a9a0707f94d94a193b8a0096e2e0e069574e708c7594ab9b1d64f96ad09",
    "tests/test_inventory.py": "0e371b8d72f1f88bf8be4e2f9300c8a5770d886562a0dc38c51c82e331181a32",
    "tests/test_notes.py": "8bf9a7c602c36ede0411e6fa838797263e4a59195eabf9aaae0c9c0ce477b502",
    "tests/test_reporting.py": "72a0ae71b5ba2a77417fb648b7f72272b0ca98c15e35602d7e45af2bec8f8e33",
    "tests/test_stocktake.py": "b6740429f3bce5c3f040223baf74d677e633ccd4d46e0af3f80755c63aabcac9",
    "tests/test_validation.py": "d7a719c468ce6529d39075d839aec1946bdb2800a33d13f374f9bb40555c694b",
    "tests/test_workflows.py": "db8229ac4e1f3a85493ddc873b6f8a20a494b2a93641d7e433d6b46991f67406",
    "verify.py": "b45a5003403a0a7b5f15907cbfc225f64aef5270fd051cd955e77130d1b52343",
}


def cli(path: Path, *args: str) -> tuple[int, str, str]:
    from io import StringIO

    from stockroom.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--db", str(path), *args], out, err)
    return code, out.getvalue(), err.getvalue()


def seed_file(path: Path) -> None:
    from stockroom.db import store
    from tests.support import seed

    with store(path) as connection:
        seed(connection)



def feature(root: Path) -> None:
    from stockroom.catalog import Catalog
    from stockroom.db import store
    from stockroom.errors import Conflict, StockroomError
    from stockroom.inventory import Inventory
    from stockroom.models import Intake
    from stockroom.reporting import Reporting

    path = root / "depot.sqlite"
    seed_file(path)
    day = "2024-05-01"
    with store(path) as db:
        inv = Inventory(db)
        inv.receive(Intake("ax", "north", "B", 5, day))
        inv.receive(Intake("ax", "north", "A", 3, "2024-04-30"))
        inv.receive(Intake("ax", "south", "B", 7, day))
    args = ("lot", "move", "AX", "north", "SOUTH", "7", day, "--memo", " signed ",
            "--request-id", "Truck_42")
    first = cli(path, *args)
    check(first[0], 0)
    with store(path) as db:
        inv = Inventory(db)
        check([row.code for row in inv.lots("AX", "SOUTH")], ["A", "B"])
        inv.ship("AX", "SOUTH", 4, day)
        Catalog(db).warehouse_status("south", False)
        after_ship = Reporting(db).activity()
    check(cli(path, *args), first)
    check(cli(path, *args[:2], "ax", "NORTH", "south", *args[5:]), first)
    with store(path) as db:
        inv = Inventory(db)
        check(Reporting(db).activity(), after_ship)
        check(str(raises(Conflict, inv.move, "ax", "NORTH", "south", 7, day,
                         "signed", request_id="Truck_42")), "request id already used")
        Catalog(db).warehouse_status("SOUTH", True)
        for malformed in ("", "bad id", "_first", "é", "x" * 65, False):
            check(str(raises(StockroomError, inv.move, "AX", "NORTH", "SOUTH", 1, day,
                             request_id=malformed)), "invalid request id")
        check(str(raises(StockroomError, inv.move, "AX", "NORTH", "SOUTH", 4, day,
                         request_id="truck_42")), "insufficient stock")
        inv.receive(Intake("AX", "NORTH", "B", 5, day))
        snapshot = inv.move("AX", "NORTH", "SOUTH", 4, day, request_id="truck_42")
        check(snapshot[0].quantity, 14)
        check(inv.move("AX", "NORTH", "SOUTH", 4, day, request_id="truck_42"), snapshot)

        # A collision on the second destination must leave no debit or receipt.
        inv.receive(Intake("SEED", "NORTH", "ONE", 2, day))
        inv.receive(Intake("SEED", "NORTH", "TWO", 2, day))
        inv.receive(Intake("SEED", "SOUTH", "TWO", 2, "2024-05-03"))
        original = (inv.lots(include_empty=True), Reporting(db).activity())
        check(str(raises(Conflict, inv.move, "SEED", "NORTH", "SOUTH", 4, day,
                         request_id="collision")), "lot details differ")
        check((inv.lots(include_empty=True), Reporting(db).activity()), original)
        check(inv.move("SEED", "NORTH", "SOUTH", 1, day, request_id="collision")[0].code, "ONE")
        check(db.execute("PRAGMA foreign_key_check").fetchall(), [])


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
