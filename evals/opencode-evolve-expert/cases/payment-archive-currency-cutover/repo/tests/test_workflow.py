import json
import subprocess
import sys

from quayside.ledger import Ledger
from quayside.model import Line

from tests.support import FIRST, MID, BookTest


class OperatorTests(BookTest):
    def test_offline_python_entry_point_reads_issued_invoice(self):
        first = self.bill(lines=[Line("Batch", 4, "1.25")])
        run = subprocess.run(
            [sys.executable, "-B", "-m", "quayside", "--ledger", str(self.path),
             "invoice", "find", "west", " ticket "],
            capture_output=True, check=True, text=True, timeout=5,
        )
        self.assertEqual(run.stderr, "")
        self.assertEqual(json.loads(run.stdout)["id"], first.id)

    def test_empty_file_never_created_by_failed_or_read_only_command(self):
        missing = self.root / "absent.json"
        run = subprocess.run(
            [sys.executable, "-B", "-m", "quayside", "--ledger", str(missing),
             "account", "list"],
            capture_output=True, check=True, text=True, timeout=5,
        )
        self.assertEqual(run.stdout, "[]\n")
        self.assertFalse(missing.exists())

    def test_mixed_archive_and_bank_paths_leave_monotonic_local_ids(self):
        first = self.deposit()
        archive = self.root / "archive.json"
        self.json_cli("archive", "export", str(archive))
        self.json_cli("archive", "import", str(archive))
        bank = self.file("bank.json", [
            {"account": "yen", "day": FIRST, "amount": "40", "reference": "bank X"},
            {"account": "west", "day": MID, "amount": "10.00", "reference": "bank Y"},
        ])
        self.json_cli("payment", "bank-import", str(bank))
        saved = Ledger(self.path).state
        self.assertEqual([row.id for row in saved.payments], [first.id, 2, 3, 4])
        shown = self.json_cli("statement", "open", FIRST, MID)
        self.assertEqual(shown["totals"], {"JPY": "-40", "USD": "-14.20"})


    def test_invalid_utf8_bank_input_is_reported_without_a_write(self):
        path = self.root / "bank.json"
        path.write_bytes(b"\xff\xfc")
        old = self.path.read_bytes()
        self.cli_error("cannot read bank.json", "payment", "bank-import", str(path))
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(Ledger(self.path).state.payments, ())

    def test_empty_and_filtered_archives_can_use_an_existing_destination(self):
        path = self.root / "export.json"
        first = self.deposit()
        self.assertEqual(self.json_cli("archive", "export", str(path), "--account", "west"),
                         {"exported": 1})
        self.assertEqual(json.loads(path.read_bytes())["payments"][0]["cents"], 210)
        self.assertEqual(self.json_cli("archive", "export", str(path), "--account", "east"),
                         {"exported": 0})
        self.assertEqual(json.loads(path.read_bytes()), {"version": 1, "payments": []})
        before = self.path.read_bytes()
        self.assertEqual(self.json_cli("archive", "import", str(path)), [])
        self.assertEqual(Ledger(self.path).state.payments, (first,))
        self.assertEqual(self.path.read_bytes(), before)

    def test_reopening_legacy_empty_collections_preserves_unknown_file_until_a_write(self):
        original = b'{"accounts":{}}\n'
        self.path.write_bytes(original)
        self.assertEqual(self.json_cli("account", "list"), [])
        self.assertEqual(self.json_cli("statement", "open", FIRST, MID), {"entries": [], "totals": {}})
        self.assertEqual(self.path.read_bytes(), original)
        self.cli_error("unknown account", "payment", "add", "west", FIRST, "1", "first")
        self.assertEqual(self.path.read_bytes(), original)
