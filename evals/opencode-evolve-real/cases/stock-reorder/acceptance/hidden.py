"""Hidden acceptance for this standalone fixture."""

import signal
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

checks = 0


def check(actual, expected):
    global checks
    expected_type = type(expected)
    assert isinstance(actual, expected_type) and actual == expected, (actual, expected)
    if expected_type in (bool, int):
        assert type(actual) is expected_type, (actual, expected)
    checks += 1


def raises(error_type, call, *args, **kwargs):
    global checks
    try:
        call(*args, **kwargs)
    except error_type as error:
        checks += 1
        return error
    raise AssertionError(f"{call!r} did not raise {error_type.__name__}")


def verify():
    for name, digest in PROTECTED.items():
        assert sha256(Path(name).read_bytes()).hexdigest() == digest, name
    result = subprocess.run([sys.executable, "-B", "verify.py"], check=False, timeout=12)
    if result.returncode != 0:
        print("Public verification failed.", file=sys.stderr)
        raise SystemExit(1)

PROTECTED = {
    'README.md': '6aadd718a6da4428027e4364acc5b75aa6a0f20721ea2da6c7889eceb675cfbe',
    'docs/commands.md': '3d3cc1837adbc85976e4ea1d56f80a4c857ce9b549c8203020cf83fea3327f3b',
    'docs/feature.md': '2934bb084b3c47a8ee1e76c500acb6a0415376aea5e3fcef23ac94b7bb804856',
    'docs/storage.md': '205b429eb4d9166d2e1396daa669a1a6ad82f6f0c403fcf0d36b9648d3014631',
    'stockroom/schema.sql': '5a79ca26adca26d15286ed5d66f421c0e03959b0205783e93533671fdb2faae0',
    'tests/__init__.py': '37c6c32e6a930b7f0021d7617b46c203dc2af08e3a7c8950c98848a3792d0e6c',
    'tests/support.py': '204a88bcda54d60c461c62f7ec7183495388958581ccbbd6f766998f4afc838a',
    'tests/test_catalog.py': '5c48c58f034004b2645b4f312c774872661d362163f55ded6a332c373059202c',
    'tests/test_cli.py': 'b01b6a82c7a1541f8c7e57b970da60b5dd6eae9329504cf709429d09816c658e',
    'tests/test_database.py': '0d80193412c0c1f58cbf18d1fb1f14907fa0cbaf45af9655426964cd29286e1f',
    'tests/test_files.py': 'b60decb00d8c80ead887c968101853bf7742126169b4eaab2f221be4270a463f',
    'tests/test_fulfillment.py': 'daaec93ff314107fef7b5f13007c74bddd7c57ade34662df1ab10dc02a7575e3',
    'tests/test_history.py': '7e712a9a0707f94d94a193b8a0096e2e0e069574e708c7594ab9b1d64f96ad09',
    'tests/test_inventory.py': '0e371b8d72f1f88bf8be4e2f9300c8a5770d886562a0dc38c51c82e331181a32',
    'tests/test_notes.py': '8bf9a7c602c36ede0411e6fa838797263e4a59195eabf9aaae0c9c0ce477b502',
    'tests/test_reporting.py': '72a0ae71b5ba2a77417fb648b7f72272b0ca98c15e35602d7e45af2bec8f8e33',
    'tests/test_stocktake.py': 'b6740429f3bce5c3f040223baf74d677e633ccd4d46e0af3f80755c63aabcac9',
    'tests/test_validation.py': 'd7a719c468ce6529d39075d839aec1946bdb2800a33d13f374f9bb40555c694b',
    'tests/test_workflows.py': 'db8229ac4e1f3a85493ddc873b6f8a20a494b2a93641d7e433d6b46991f67406',
    'verify.py': 'a4eb276b9d91ebc8508b5aee42ef3cce221b18f5f5e1fabcd846ad29faa0b7d3',
}


def legacy(path):
    import sqlite3

    with sqlite3.connect(path) as original:
        original.executescript(Path("stockroom/schema.sql").read_text(encoding="utf-8"))
        original.execute("INSERT INTO warehouse VALUES ('NORTH','North',1),('SOUTH','South',1)")
        original.execute("INSERT INTO item VALUES ('AX','Axes','TOOLS','each',1),('SEED','Seeds','GARDEN','box',1)")
        original.execute("INSERT INTO supplier VALUES ('COOP','Supplies','',1)")
        original.execute("INSERT INTO lot VALUES (41,'AX','NORTH','LEGACY',3,'2024-02-28',NULL)")
        original.execute("INSERT INTO movement VALUES (7,'AX','NORTH',41,'2024-02-28','receive',3,NULL,'prior')")
        original.execute("INSERT INTO note VALUES (2,41,'2024-02-28','old note',0)")


def command(path, *arguments):
    from io import StringIO

    from stockroom.cli import run

    out, err = StringIO(), StringIO()
    code = run(["--db", str(path), *arguments], out, err)
    return code, out.getvalue(), err.getvalue()


def json_command(path, *arguments):
    import json

    code, output, error = command(path, *arguments)
    assert code == 0 and error == "" and output.endswith("\n"), (arguments, code, output, error)
    return json.loads(output)


def error_command(path, message, *arguments):
    check(command(path, *arguments), (2, "", f"error: {message}\n"))


def check_feature():
    import csv
    from io import StringIO
    from tempfile import TemporaryDirectory

    from stockroom.db import store
    from stockroom.models import ReorderLine, ReorderRule
    from stockroom.reporting import Reporting

    with TemporaryDirectory(prefix="reorder-") as scratch:
        root = Path(scratch)
        path = root / "legacy.sqlite"
        legacy(path)
        old = json_command(path, "report", "activity")[0]
        check((old["id"], old["delta"], old["memo"]), (7, 3, "prior"))
        check(json_command(path, "report", "reorder", "--all"), [])
        check(command(path, "report", "reorder", "--format", "csv"),
              (0, "sku,warehouse,minimum,available,shortage\r\n", ""))
        for sku, warehouse, minimum in [("seed", "south", 0), ("ax", "south", 2),
                                        ("seed", "north", 1), ("ax", "north", 5)]:
            check(json_command(path, "item", "reorder-set", sku, warehouse, str(minimum)),
                  {"sku": sku.upper(), "warehouse": warehouse.upper(), "minimum": minimum})
        check(json_command(path, "report", "activity"), [old])
        check(json_command(path, "report", "balances"),
              [{"sku": "AX", "warehouse": "NORTH", "quantity": 3, "positive_lots": 1}])

        def row(sku, warehouse, minimum, available):
            return {"sku": sku, "warehouse": warehouse, "minimum": minimum,
                    "available": available, "shortage": max(0, minimum - available)}

        north_ax = row("AX", "NORTH", 5, 3)
        north_seed = row("SEED", "NORTH", 1, 0)
        south_ax = row("AX", "SOUTH", 2, 0)
        south_seed = row("SEED", "SOUTH", 0, 0)
        check(json_command(path, "report", "reorder"), [north_ax, north_seed, south_ax])
        check(json_command(path, "report", "reorder", "--all"), [north_ax, north_seed, south_ax, south_seed])
        check(json_command(path, "report", "reorder", "--sku", "AX"), [north_ax, south_ax])
        check(json_command(path, "report", "reorder", "--warehouse", "south", "--all"), [south_ax, south_seed])
        check(json_command(path, "report", "reorder", "--warehouse", "south", "--sku", "SEED"), [])
        check(json_command(path, "report", "reorder", "--warehouse", "south", "--sku", "SEED", "--all"), [south_seed])
        check(json_command(path, "report", "reorder", "--warehouse", "UNKNOWN", "--all"), [])
        check(json_command(path, "report", "reorder", "--sku", "UNKNOWN", "--all"), [])
        error_command(path, "invalid sku", "report", "reorder", "--sku", "a b")
        error_command(path, "invalid warehouse", "report", "reorder", "--warehouse", "")

        with store(path) as db:
            check(db.execute("PRAGMA user_version").fetchone()[0], 2)
            reporting = Reporting(db)
            check(reporting.set_reorder("aX", "north", 3), ReorderRule("AX", "NORTH", 3))
            check(reporting.reorder("ax", "north"), [])
            check(reporting.reorder("ax", "north", include_healthy=True), [ReorderLine("AX", "NORTH", 3, 3, 0)])
            for invalid in (-1, False, 2**63, 1.0, "2", None):
                assert str(raises(ValueError, reporting.set_reorder, "AX", "NORTH", invalid)) == "invalid minimum"
        error_command(path, "unknown sku", "item", "reorder-set", "new", "none", "-1")
        error_command(path, "unknown warehouse", "item", "reorder-clear", "ax", "none")
        error_command(path, "invalid minimum", "item", "reorder-set", "ax", "north", "-1")
        check(json_command(path, "report", "reorder", "--warehouse", "NORTH", "--all"),
              [row("AX", "NORTH", 3, 3), north_seed])

        json_command(path, "warehouse", "status", "south", "inactive")
        json_command(path, "item", "status", "seed", "inactive")
        check(json_command(path, "report", "reorder", "--all"), [row("AX", "NORTH", 3, 3)])
        check(json_command(path, "item", "reorder-set", "seed", "south", "4")["minimum"], 4)
        json_command(path, "warehouse", "status", "south", "active")
        check(json_command(path, "report", "reorder", "--warehouse", "south", "--all"), [south_ax])
        json_command(path, "item", "status", "seed", "active")
        south_seed = row("SEED", "SOUTH", 4, 0)
        check(json_command(path, "report", "reorder", "--warehouse", "south", "--all"), [south_ax, south_seed])
        check(json_command(path, "item", "reorder-clear", "ax", "south"), {"removed": True})
        check(json_command(path, "item", "reorder-clear", "ax", "south"), {"removed": False})

        json_command(path, "lot", "receive", "ax", "north", "N2", "10", "2024-03-03")
        json_command(path, "lot", "ship", "ax", "north", "3", "2024-03-04")
        json_command(path, "lot", "move", "ax", "north", "south", "9", "2024-03-04")
        check(json_command(path, "report", "reorder", "--sku", "ax", "--all"), [row("AX", "NORTH", 3, 1)])
        result = subprocess.run([sys.executable, "-B", "-m", "stockroom", "--db", str(path),
                                 "report", "reorder", "--all", "--format", "csv"],
                                capture_output=True, check=False, timeout=3)
        check((result.returncode, result.stderr), (0, b""))
        check(result.stdout, b"sku,warehouse,minimum,available,shortage\r\nAX,NORTH,3,1,2\r\n"
                             b"SEED,NORTH,1,0,1\r\nSEED,SOUTH,4,0,4\r\n")
        check([entry["warehouse"] for entry in csv.DictReader(StringIO(result.stdout.decode()))],
              ["NORTH", "NORTH", "SOUTH"])

        fresh = root / "fresh.sqlite"
        with store(fresh) as db:
            from stockroom.inventory import Inventory
            from stockroom.models import Intake
            from tests.support import seed
            seed(db)
            limit = 2**63 - 1
            inventory = Inventory(db)
            inventory.receive(Intake("SEED", "SOUTH", "BIG", limit, "2024-01-01"))
            inventory.receive(Intake("SEED", "SOUTH", "SMALL", 2, "2024-01-01"))
            reporting = Reporting(db)
            reporting.set_reorder("SEED", "SOUTH", limit)
            check(reporting.reorder(include_healthy=True), [ReorderLine("SEED", "SOUTH", limit, limit + 2, 0)])
            check(db.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
