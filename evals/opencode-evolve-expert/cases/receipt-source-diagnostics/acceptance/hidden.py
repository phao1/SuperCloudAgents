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
    "docs/adr/018-release.md": "818fac9c2fa310798b482b2424adfdd360806c127acac3713e3fcd4731e8a5d3",
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
    "verify.py": "3eeabcef5feae4557620c59e21d4b3a8d14a830782f41a767a4b4eb5c2485d36",
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
    from stockroom.errors import Conflict, StockroomError
    from stockroom.files import csv_requests, import_receipts
    from stockroom.inventory import Inventory
    from stockroom.models import Intake
    from stockroom.reporting import Reporting

    filename, source = root / "depot.sqlite", root / "supplier.csv"
    seed_file(filename)
    day = "2024-05-01"
    header = "memo,warehouse,code,quantity,received,sku,supplier\r\n"
    with store(filename) as db:
        initial = Inventory(db).receive(Intake("AX", "NORTH", "EXISTING", 2, day))
        before = Reporting(db).activity()
        first = f'"frozen\r\n pallet",NORTH,NEW,4,{day},AX,COOP\r\n'
        erroneous = '"notes\r\nhere",NORTH,EXISTING,3,2024-05-02,AX,\r\n'
        source.write_bytes((header + first + "\r\n" + erroneous).encode())
        error = raises(Conflict, import_receipts, db, source)
        check(str(error), "receipt line 5: lot details differ")
        check(Inventory(db).lots(), [initial])
        check(Reporting(db).activity(), before)

        source.write_bytes((header + first + '\r\n"unclosed\r\nline').encode())
        check(str(raises(StockroomError, import_receipts, db, source)), "invalid receipt line 5")
        check(Inventory(db).lots(), [initial])
        source.write_bytes((header + f'"a\r\nb",NORTH,NEW,invalid,{day},AX,COOP\r\n').encode())
        check(str(raises(StockroomError, import_receipts, db, source)), "invalid receipt line 2")
        source.write_bytes((header + first + f"note,BAD,X,1,{day},AX,\r\nshort\r\n").encode())
        check(str(raises(StockroomError, import_receipts, db, source)), "invalid receipt line 5")
        check(Inventory(db).lots(), [initial])

        source.write_bytes((header + first).encode("utf-8-sig"))
        ok = cli(filename, "file", "import-receipts", str(source))
        check((ok[0], ok[2]), (0, ""))
        check(json.loads(ok[1])[0]["code"], "NEW")
        check(Reporting(db).activity()[-1].memo, "frozen\r\n pallet")
        [record] = csv_requests((header + first).splitlines(keepends=True))
        check(record, Intake("AX", "NORTH", "NEW", 4, day, "COOP", "frozen\r\n pallet"))
        source.write_bytes(b"\xff\xfef")
        check(str(raises(StockroomError, import_receipts, db, source)), "invalid receipt file")
        check(str(raises(Conflict, Inventory(db).receive_all,
                         [Intake("AX", "NORTH", "EXISTING", 1, "2024-05-02")])),
              "lot details differ")


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
