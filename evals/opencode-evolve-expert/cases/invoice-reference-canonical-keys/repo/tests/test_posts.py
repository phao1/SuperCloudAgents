from decimal import Decimal
from unittest.mock import patch

from quayside import accounts, invoices, lookup, payments, storage
from quayside.errors import LedgerError
from quayside.ledger import Ledger
from quayside.model import Line

from tests.support import FIRST, MID, BookTest


class PostingTests(BookTest):
    def test_account_persists_name_and_normalizes_code_and_currency(self):
        value = self.json_cli("account", "put", "wEst", "Marché de rue", "usd")
        self.assertEqual(value, {"code": "WEST", "name": "Marché de rue", "currency": "USD"})
        self.assertEqual([row["code"] for row in self.json_cli("account", "list")],
                         ["EAST", "KW", "WEST", "YEN"])
        self.assertEqual(accounts.get(Ledger(self.path).state, "west").name, "Marché de rue")

    def test_currency_change_is_rejected_even_for_empty_accounts(self):
        before = self.path.read_bytes()
        self.cli_error("account currency cannot change", "account", "put", "west", "Other", "CHF")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(accounts.get(Ledger(self.path).state, "west").currency, "USD")

    def test_issue_preserves_line_order_reference_and_native_wire(self):
        submitted = self.file("invoice.json", [
            {"description": "East shed", "units": 2, "unit_price": "1.0000"},
            {"description": "Bakery – small", "units": 3, "unit_price": "0.125"},
        ])
        projection = self.json_cli("invoice", "issue", "kw", " June, 8 ", FIRST, str(submitted))
        self.assertEqual((projection["reference"], projection["account"]), (" June, 8 ", "KW"))
        self.assertEqual([row["unit_price"] for row in projection["lines"]], ["1.000", "0.125"])
        self.assertEqual(self.json_cli("invoice", "show", "1"), projection)
        self.assertEqual(invoices.total(invoices.get(Ledger(self.path).state, 1)), Decimal("2.375"))

    def test_reference_uniqueness_is_scoped_by_account(self):
        west = self.bill(reference=" Paper-14 ")
        east = self.bill(account="east", reference="pAPEr-14")
        self.assertEqual(lookup.find(self.ledger.state, "WEST", "paper-14"), west)
        self.assertEqual(lookup.find(self.ledger.state, "East", " Paper-14"), east)
        with self.assertRaisesRegex(LedgerError, "invoice reference already exists"):
            self.bill(reference="PAPER-14")
        self.assertEqual([row["id"] for row in self.json_cli("invoice", "list", "--account", "east")], [2])

    def test_existing_invoice_lookup_failure_does_not_change_file(self):
        self.bill()
        before = self.path.read_bytes()
        self.cli_error("unknown invoice", "invoice", "show", "33")
        self.cli_error("unknown invoice reference", "invoice", "find", "east", "Ticket")
        self.cli_error("unknown account", "invoice", "find", "missing", "Ticket")
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_items_do_not_allocate_an_invoice_id(self):
        first = self.bill()
        for entries, error in [
            ([], "invoice needs a line"),
            ([Line("tote", True, "2.00")], "invalid units"),
            ([Line("tote", 0, "2.00")], "invalid units"),
            ([Line("tote", 3, "-2.00")], "negative unit price"),
            ([Line("tote", 3, "2.001")], "currency precision"),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(LedgerError, error):
                self.bill(reference="bad", lines=iter(entries))
        next_ = self.bill(reference="next")
        self.assertEqual(next_.id, first.id + 1)

    def test_invalid_invoice_file_shape_does_not_write(self):
        for payload, message in [({}, "invalid invoice file"), ([None], "invalid invoice line"),
                                 ([{"units": 8}], "invalid invoice line")]:
            with self.subTest(payload=payload):
                path = self.file("invoice.json", payload)
                self.cli_error(message, "invoice", "issue", "west", "Title", FIRST, str(path))
        self.assertEqual(self.json_cli("invoice", "list"), [])

    def test_signed_payments_keep_distinct_registration_ids(self):
        first = self.deposit(amount="4", reference="bank chèque")
        second = self.deposit(amount="-2.00", reference="bank chèque")
        other = self.deposit(account="east")
        self.assertEqual((first.id, second.id, other.id), (1, 2, 3))
        self.assertEqual((first.amount, second.amount), ("4.00", "-2.00"))
        self.assertEqual(payments.listing(Ledger(self.path).state, "west"), [first, second])

    def test_ignored_zero_deposits_are_errors_and_do_not_change_state(self):
        before = self.path.read_bytes()
        self.cli_error("payment cannot be zero", "payment", "add", "west", MID, "-0.000", "Void")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.deposit().id, 1)

    def test_storage_write_failure_does_not_apply_payment_in_memory(self):
        previous = self.ledger.state
        before = self.path.read_bytes()
        with patch.object(storage, "write", side_effect=LedgerError("full disk")), self.assertRaisesRegex(
            LedgerError, "full disk",
        ):
            self.deposit()
        self.assertEqual(self.ledger.state, previous)
        self.assertEqual(self.path.read_bytes(), before)
