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
    'docs/feature.md': '7aa162169eadc8ce94d1dcfb2541d47561f6a4851167ed3e9a6aa84dae8026e0',
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
    'verify.py': '012c9fea21bc65a3096762bb590dabda5d944158f766181bf584557f4940b998',
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
    from stockroom.inventory import Inventory
    from stockroom.models import Intake

    with TemporaryDirectory(prefix="expiry-") as scratch:
        root = Path(scratch)
        path = root / "legacy.sqlite"
        legacy(path)
        with store(path) as db:
            check(db.execute("PRAGMA user_version").fetchone()[0], 2)
            [old] = Inventory(db).lots()
            check((old.id, old.expires_on, old.quantity), (41, None, 3))
        [old] = json_command(path, "lot", "list")
        check(old, {"id": 41, "sku": "AX", "warehouse": "NORTH", "code": "LEGACY", "quantity": 3,
                    "received": "2024-02-28", "supplier": None})
        check(json_command(path, "note", "list", "41")[0]["text"], "old note")
        new = json_command(path, "lot", "receive", "ax", "north", "L4", "3", "2024-02-29",
                           "--expires-on", "2024-03-01", "--supplier", "Coop")
        check((new["expires_on"], new["quantity"], new["supplier"]), ("2024-03-01", 3, "COOP"))
        json_command(path, "lot", "receive", "AX", "NORTH", "future", "2", "2024-03-10")
        json_command(path, "lot", "receive", "AX", "NORTH", "boundary", "2", "2024-02-29",
                     "--expires-on", "2024-02-29")
        json_command(path, "lot", "adjust", "AX", "NORTH", "boundary", "-2", "2024-03-01")
        json_command(path, "lot", "receive", "SEED", "NORTH", "seeds", "2", "2024-02-29")
        json_command(path, "lot", "receive", "AX", "SOUTH", "other", "1", "2024-02-28")
        for query_day, include in [
            ("2024-02-27", []), ("2024-02-28", ["LEGACY"]), ("2024-02-29", ["LEGACY", "L4", "BOUNDARY"]),
            ("2024-03-01", ["LEGACY", "L4"]), ("2024-03-02", ["LEGACY"]),
            ("2024-03-10", ["LEGACY", "FUTURE"]),
        ]:
            params = ("--sku", "ax", "--warehouse", "north", "--usable-on", query_day)
            found = json_command(path, "lot", "list", "--include-empty", *params)
            check([row["code"] for row in found], include)
            check([row["code"] for row in json_command(path, "lot", "export", *params)],
                  [code for code in include if code != "BOUNDARY"])
            with store(path) as db:
                check([row.code for row in Inventory(db).lots("AX", "NORTH", True, usable_on=query_day)], include)
        legacy_header = "id,sku,warehouse,code,quantity,received,supplier"
        code, wire, err = command(path, "lot", "export", "--format", "csv", "--with-expiry",
                                  "--sku", "AX", "--warehouse", "NORTH", "--usable-on", "2024-03-01")
        assert (code, err) == (0, "")
        check(wire.split("\r\n")[0], legacy_header + ",expires_on")
        check([row["expires_on"] for row in csv.DictReader(StringIO(wire))], ["", "2024-03-01"])
        check(command(path, "lot", "export", "--format", "csv", "--usable-on", "2024-02-01"),
              (0, legacy_header + "\r\n", ""))
        check(command(path, "lot", "list", "--format", "csv", "--with-expiry", "--usable-on", "2024-02-01"),
              (0, legacy_header + ",expires_on\r\n", ""))

        before = json_command(path, "report", "activity")
        for value, message in [("2024-2-29", "invalid day"), ("2023-02-29", "invalid day"),
                               ("2024-02-28", "expiry before receipt"), ("2024-03-02", "lot details differ")]:
            error_command(path, message, "lot", "receive", "AX", "NORTH", "L4", "3", "2024-02-29",
                          "--supplier", "COOP", "--expires-on", value)
        error_command(path, "lot details differ", "lot", "receive", "AX", "NORTH", "L4", "3", "2024-02-29",
                      "--supplier", "COOP")
        error_command(path, "invalid day", "lot", "list", "--usable-on", "2024-02-30")
        check(json_command(path, "report", "activity"), before)
        received_again = json_command(path, "lot", "receive", "AX", "NORTH", "L4", "1", "2024-02-29",
                                      "--expires-on", "2024-03-01", "--supplier", "COOP")
        check((received_again["id"], received_again["quantity"], received_again["expires_on"]),
              (new["id"], 4, "2024-03-01"))

        csv_file = root / "incoming.csv"
        header = "sku,warehouse,code,quantity,received,expires_on\n"
        csv_file.write_text(header + "SEED,SOUTH,C,3,2024-02-29,2024-03-10\nSEED,SOUTH,D,2,2024-02-28,\n",
                            encoding="utf-8")
        received = json_command(path, "file", "import-receipts", str(csv_file))
        check([row.get("expires_on") for row in received], ["2024-03-10", None])
        csv_file.write_text(header + "SEED,SOUTH,N,3,2024-02-29,2024-03-10\nSEED,SOUTH,C,2,2024-02-29,\n",
                            encoding="utf-8")
        error_command(path, "lot details differ", "file", "import-receipts", str(csv_file))
        check([row["code"] for row in json_command(path, "lot", "list", "--sku", "seed", "--warehouse", "south")],
              ["D", "C"])

        received_at_destination = json_command(path, "lot", "receive", "ax", "south", "L4", "2", "2024-02-29",
                                               "--expires-on", "2024-03-02", "--supplier", "COOP")
        before = json_command(path, "report", "activity")
        error_command(path, "lot details differ", "lot", "move", "AX", "NORTH", "SOUTH", "4", "2024-03-05")
        check(json_command(path, "report", "activity"), before)
        check(json_command(path, "lot", "list", "--sku", "ax", "--warehouse", "north")[0]["quantity"], 3)
        check(json_command(path, "lot", "list", "--sku", "ax", "--warehouse", "south")[-1], received_at_destination)

        fresh = root / "fresh.sqlite"
        with store(fresh) as db:
            from tests.support import seed
            seed(db)
            inventory = Inventory(db)
            inventory.receive(Intake("AX", "NORTH", "A", 12, "2024-02-29", expires_on="2024-03-01"))
            check(inventory.move("AX", "NORTH", "SOUTH", 3, "2024-03-05")[0].expires_on, "2024-03-01")
            check(inventory.ship("AX", "NORTH", 4, "2024-03-05")[0].expires_on, "2024-03-01")
            check(db.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    print(f"{checks} feature checks passed.")
