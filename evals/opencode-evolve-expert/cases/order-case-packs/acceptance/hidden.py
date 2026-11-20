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
    "docs/adr/018-release.md": "a4eda51aa356cdfda0d32aa2ae3902f8c5a403c67b2a9a50985cd97c2e5ed452",
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
    "verify.py": "995da9083cf091c86f41a0b67d01c167c1a5487dc870612e1ce1656d41270d7f",
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
    import json

    from stockroom.db import store
    from stockroom.errors import StockroomError
    from stockroom.fulfillment import Fulfillment, PackDemand

    db, order = root / "depot.sqlite", root / "order.json"
    seed_file(db)
    for site, code, amount, day in [
        ("SOUTH", "S", "7", "2024-05-04"), ("NORTH", "A", "2", "2024-05-03"),
        ("NORTH", "FIRST", "3", "2024-05-01"), ("NORTH", "B", "1", "2024-05-03"),
    ]:
        check(cli(db, "lot", "receive", "ax", site, code, amount, day)[0], 0)
    old = cli(db, "report", "activity")
    order.write_text(json.dumps([
        {"sku": "ax", "packs": 1, "size": 4},
        {"sku": "AX", "packs": 3, "size": 3},
        {"sku": "ax", "packs": 2, "size": 1},
    ]))
    args = ("order", "pack", str(order), "--warehouse", "south", "--warehouse", "north")
    code, out, error = cli(db, *args)
    check((code, error), (0, ""))
    plan = json.loads(out)
    check([(p["line"], p["warehouse"], p["lot"], p["lot_id"], p["quantity"])
           for p in plan["picks"]], [
        (1, "SOUTH", "S", 1, 4),
        (2, "SOUTH", "S", 1, 3), (2, "NORTH", "FIRST", 3, 3),
        (2, "NORTH", "A", 2, 2), (2, "NORTH", "B", 4, 1),
    ])
    check(plan["shortages"], [{"line": 3, "sku": "AX", "missing_packs": 2}])
    order.write_text('[{"sku":"AX","packs":2,"size":5}]')
    split = json.loads(cli(db, "order", "pack", str(order))[1])
    check([(p["warehouse"], p["quantity"]) for p in split["picks"]],
          [("NORTH", 3), ("NORTH", 2), ("SOUTH", 5)])
    order.write_text('[{"sku":"AX","packs":1,"size":8}]')
    check(json.loads(cli(db, "order", "pack", str(order))[1]), {
        "picks": [], "shortages": [{"line": 1, "sku": "AX", "missing_packs": 1}],
    })
    check(cli(db, "report", "activity"), old)
    with store(db) as connection:
        pack = Fulfillment(connection).pack
        check(pack(iter([])).picks, [])
        for count, size in ((True, 2), (1, 0), (2**62, 3)):
            check(str(raises(StockroomError, pack, [PackDemand("AX", count, size)])), "invalid quantity")
        check(str(raises(StockroomError, pack, [], ["south", "SOUTH"])), "duplicate warehouse")
    order.write_text('[{"sku":"AX","packs":1,"quantity":3}]')
    check(cli(db, "order", "pack", str(order)),
          (2, "", "error: invalid pack order row\n"))
    check(cli(db, "lot", "receive", "seed", "north", "BULK", "100000000000", "2024-05-01")[0], 0)
    order.write_text('[{"sku":"SEED","packs":10000000000,"size":3}]')
    bulk = json.loads(cli(db, "order", "pack", str(order))[1])
    check([(p["lot"], p["quantity"]) for p in bulk["picks"]], [("BULK", 30000000000)])


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
