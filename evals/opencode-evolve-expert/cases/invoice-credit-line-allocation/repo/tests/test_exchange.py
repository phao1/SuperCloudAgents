import json

from quayside import exchange, payments, storage
from quayside.errors import LedgerError
from quayside.ledger import Ledger

from tests.support import FIRST, LAST, MID, BookTest


class BankTests(BookTest):
    def row(self, *, account="west", amount="2.30", date=FIRST, reference="Bank"):
        return {"account": account, "day": date, "amount": amount, "reference": reference}

    def test_bank_file_preserves_order_and_normalizes_account_values(self):
        file = self.file("bank.json", [
            self.row(amount="-3.20", date=LAST, reference="chargeback"),
            self.row(account="EAST", amount="1.00", reference="paiement, café"),
        ])
        came = self.json_cli("payment", "bank-import", str(file))
        self.assertEqual([(row["id"], row["account"], row["day"], row["amount"]) for row in came],
                         [(1, "WEST", LAST, "-3.20"), (2, "EAST", FIRST, "1.00")])
        self.assertEqual(self.json_cli("payment", "list"), came)

    def test_remittance_repeated_refs_are_distinct_legacy_deposits(self):
        file = self.file("bank.json", [self.row(), self.row()])
        first = self.ledger.change(payments.import_bank, file)
        second = self.ledger.change(payments.import_bank, file)
        self.assertEqual([row.id for row in [*first, *second]], [1, 2, 3, 4])
        self.assertEqual({row.reference for row in first + second}, {"Bank"})

    def test_bad_late_bank_row_rolls_back_prior_item(self):
        original = self.deposit(reference="On file")
        before = self.path.read_bytes()
        file = self.file("bank.json", [self.row(), self.row(account="missing")])
        self.cli_error("unknown account", "payment", "bank-import", str(file))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(payments.listing(Ledger(self.path).state), [original])
        self.assertEqual(self.deposit().id, 2)

    def test_bank_file_must_have_valid_json_rows(self):
        for data in (None, {}, [None], [self.row() | {"value": "2"}]):
            with self.subTest(data=data):
                file = self.file("bank.json", data)
                self.cli_error("invalid remittance rows", "payment", "bank-import", str(file))
        self.assertEqual(self.ledger.state.payments, ())

    def test_empty_bank_batch_leaves_existing_file_unchanged(self):
        before = self.path.read_bytes()
        file = self.file("bank.json", [])
        self.assertEqual(self.ledger.change(payments.import_bank, file), [])
        self.assertEqual(self.path.read_bytes(), before)


class ArchiveTests(BookTest):
    def test_v1_writer_keeps_integer_cents_and_omits_ids(self):
        self.deposit(amount="2.50", reference="réf")
        self.deposit(account="east", amount="-4.10")
        path = self.root / "out.json"
        self.assertEqual(self.json_cli("archive", "export", str(path)), {"exported": 2})
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw, {"version": 1, "payments": [
            {"account": "WEST", "day": FIRST, "reference": "réf", "cents": 250},
            {"account": "EAST", "day": FIRST, "reference": "bank", "cents": -410},
        ]})
        self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_import_allocates_fresh_ids_in_archive_order(self):
        existing = self.deposit()
        path = self.file("in.json", {"version": 1, "payments": [
            {"account": "west", "day": MID, "reference": "first", "cents": 3},
            {"account": "east", "day": FIRST, "reference": "next", "cents": -700},
        ]})
        received = self.ledger.change(exchange.restore, path)
        self.assertEqual([(row.id, row.amount, row.day) for row in received],
                         [(2, "0.03", MID), (3, "-7.00", FIRST)])
        self.assertEqual(Ledger(self.path).state.payments, (existing, *received))

    def test_filtered_export_is_read_only_and_keeps_order(self):
        first = self.deposit(day=LAST)
        self.deposit(account="east", day=FIRST)
        last = self.deposit(day=FIRST, reference="early recorded late")
        before = self.path.read_bytes()
        path = self.root / "out.json"
        self.assertEqual(exchange.dump(self.ledger.state, path, "west"), 2)
        self.assertEqual([row["day"] for row in storage.read(path)["payments"]], [first.day, last.day])
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_archive_version_and_integer_types_are_errors(self):
        row = {"account": "east", "day": FIRST, "reference": "old", "cents": 20}
        for data, message in [(None, "invalid archive version or shape"),
                              ({"version": True, "payments": [row]}, "invalid archive version or shape"),
                              ({"version": 7, "payments": [row]}, "invalid archive version or shape"),
                              ({"version": 1, "payments": [row | {"cents": True}]}, "invalid archive payment")]:
            with self.subTest(data=data):
                file = self.file("bad.json", data)
                self.cli_error(message, "archive", "import", str(file))
        self.assertEqual(self.json_cli("payment", "list"), [])

    def test_late_archive_validation_keeps_existing_rows(self):
        row = {"account": "west", "day": FIRST, "reference": "new", "cents": 150}
        original = self.deposit()
        path = self.file("bad.json", {"version": 1, "payments": [row, row | {"day": "2024-13-01"}]})
        with self.assertRaisesRegex(LedgerError, "invalid day"):
            self.ledger.change(exchange.restore, path)
        self.assertEqual(Ledger(self.path).state.payments, (original,))

    def test_duplicate_json_keys_are_rejected_even_in_valid_archive_shape(self):
        path = self.root / "dup.json"
        path.write_text('{"version":1,"version":1,"payments":[]}', encoding="utf-8")
        self.cli_error("invalid JSON in dup.json", "archive", "import", str(path))
