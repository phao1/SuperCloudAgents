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
    'docs/feature.md': '3e19a2ae13fbf182c30d25df66783256d0c81b4a1017c5b6fca7c4899ca9bbc3',
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
    'verify.py': 'db58ed9ddf14387cee3299acc39d2a04961cba65cfa31cc9061bd7021e94e0fb',
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

    from stockroom.catalog import Catalog
    from stockroom.db import store
    from stockroom.inventory import Inventory
    from stockroom.models import Intake
    from stockroom.reporting import Reporting

    with TemporaryDirectory(prefix="rename-") as scratch:
        root = Path(scratch)
        path = root / "old.sqlite"
        legacy(path)
        day = "2024-02-28"
        original_lot = json_command(path, "lot", "list")[0]
        original_event = json_command(path, "report", "activity")[0]
        note = json_command(path, "note", "list", "41")
        stocktake = json_command(path, "count", "open", "north", "before label change", day)
        check(json_command(path, "item", "aliases", "ax"), {"sku": "AX", "aliases": []})
        item = json_command(path, "item", "show", "ax")
        check(json_command(path, "item", "rename", "ax", "AX"), item)
        error_command(path, "unknown sku", "item", "rename", "missing", "AAA")
        error_command(path, "invalid sku", "item", "rename", "missing", "bad code")
        error_command(path, "sku already exists", "item", "rename", "AX", "seed")
        check(json_command(path, "lot", "list"), [original_lot])

        check(json_command(path, "item", "rename", "Ax", "z-new"), {**item, "sku": "Z-NEW"})
        check(json_command(path, "item", "rename", "ax", "a-current"), {**item, "sku": "A-CURRENT"})
        for spelling in ("aX", "z-new", "a-current"):
            check(json_command(path, "item", "show", spelling), {**item, "sku": "A-CURRENT"})
            check(json_command(path, "item", "aliases", spelling), {"sku": "A-CURRENT", "aliases": ["AX", "Z-NEW"]})
            for args in [("lot", "list"), ("lot", "export")]:
                check(json_command(path, *args, "--sku", spelling), [{**original_lot, "sku": "A-CURRENT"}])
            check(json_command(path, "report", "activity", "--sku", spelling),
                  [{**original_event, "sku": "A-CURRENT"}])
            for report in ("balances", "audit", "trend"):
                check(json_command(path, "report", report, "--sku", spelling)[0]["sku"], "A-CURRENT")
            for report in ("as-of", "aging"):
                check(json_command(path, "report", report, day, "--sku", spelling)[0]["sku"], "A-CURRENT")
        check(json_command(path, "item", "rename", "z-new", "a-current"), {**item, "sku": "A-CURRENT"})
        check(json_command(path, "note", "list", "41"), note)
        check(json_command(path, "count", "sheet", str(stocktake["id"]))[0]["sku"], "A-CURRENT")

        status = json_command(path, "item", "status", "Ax", "inactive")
        check(status, {**item, "sku": "A-CURRENT", "active": False})
        check(json_command(path, "item", "status", "z-NEW", "active"), {**item, "sku": "A-CURRENT"})
        error_command(path, "sku already exists", "item", "add", "ax", "Sideways")
        error_command(path, "sku already exists", "item", "rename", "seed", "z-new")
        error_command(path, "sku already exists", "item", "rename", "a-current", "ax")
        catalog = root / "conflict.json"
        catalog.write_text('{"warehouses":[{"code":"NEW","name":"new"}],"items":[{"sku":"ax","name":"Axes"}]}',
                           encoding="utf-8")
        error_command(path, "sku already exists", "file", "import-catalog", str(catalog))
        check([row["code"] for row in json_command(path, "warehouse", "list")], ["NORTH", "SOUTH"])
        check([row["sku"] for row in json_command(path, "file", "export-catalog")["items"]], ["A-CURRENT", "SEED"])

        updated = json_command(path, "lot", "receive", "z-new", "north", "legacy", "2", day)
        check((updated["id"], updated["sku"], updated["quantity"]), (41, "A-CURRENT", 5))
        check(json_command(path, "lot", "adjust", "ax", "north", "legacy", "1", day)["quantity"], 6)
        check(json_command(path, "lot", "move", "z-new", "north", "south", "3", day)[0]["sku"], "A-CURRENT")
        check(json_command(path, "lot", "ship", "ax", "south", "1", day)[0]["quantity"], 2)
        receipt = root / "incoming.csv"
        receipt.write_text("sku,warehouse,code,quantity,received\nax,south,new,9,2024-03-01\n", encoding="utf-8")
        check(json_command(path, "file", "import-receipts", str(receipt))[0]["sku"], "A-CURRENT")
        order = root / "order.json"
        order.write_text('[{"sku":"ax","quantity":1},{"sku":"a-current","quantity":2}]', encoding="utf-8")
        plan = json_command(path, "order", "plan", str(order), "--warehouse", "north")
        check(plan, {"picks": [{"sku": "A-CURRENT", "warehouse": "NORTH", "lot": "LEGACY",
                                "lot_id": 41, "quantity": 3}], "shortages": []})
        code, wire, error = command(path, "lot", "export", "--sku", "AX", "--format", "csv")
        check((code, error), (0, ""))
        check({row["sku"] for row in csv.DictReader(StringIO(wire))}, {"A-CURRENT"})
        check(json_command(path, "lot", "list", "--sku", "missing"), [])
        check(json_command(path, "report", "activity", "--sku", "missing"), [])
        with store(path) as db:
            check(db.execute("PRAGMA user_version").fetchone()[0], 2)
            check(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            check([row[0] for row in db.execute("SELECT DISTINCT sku FROM movement")], ["A-CURRENT"])
            check(Catalog(db).aliases("ax"), ["AX", "Z-NEW"])

        fresh = root / "fresh.sqlite"
        with store(fresh) as db:
            from tests.support import seed
            seed(db)
            catalog, inventory = Catalog(db), Inventory(db)
            catalog.item_status("SEED", False)
            check(catalog.rename_item("SEED", "seed-1").active, False)
            check(catalog.aliases("seed"), ["SEED"])
            check(catalog.item_status("seed", True).sku, "SEED-1")
            check(inventory.receive(Intake("seed", "NORTH", "Y", 9, day)).sku, "SEED-1")
            check(Reporting(db).balances(sku="SEED")[0].quantity, 9)
            check(db.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
