import csv
from io import StringIO
from unittest.mock import patch

from quayside import register, storage
from quayside.errors import LedgerError
from quayside.ledger import Ledger

from tests.support import FIRST, LAST, MID, BookTest

HEADER = "day,id,account,currency,reference,amount\r\n"


class RegisterTests(BookTest):
    def run_export(self, name="register.csv", *flags):
        path = self.root / name
        return self.json_cli("payment", "register", str(path), *flags), path.read_bytes()

    def test_register_orders_day_first_then_local_integer_id(self):
        first = self.deposit(day=LAST)
        second = self.deposit(account="yen", amount="7")
        third = self.deposit(account="kw", amount="-1.300")
        self.deposit(account="east", amount="0.25", day=MID)
        reply, binary = self.run_export()
        self.assertEqual(reply, {"exported": 4})
        actual = list(csv.DictReader(StringIO(binary.decode(), newline="")))
        self.assertEqual([int(row["id"]) for row in actual], [second.id, third.id, 4, first.id])
        self.assertEqual([row["amount"] for row in actual], ["7", "-1.300", "0.25", "2.10"])
        self.assertEqual([row["currency"] for row in actual], ["JPY", "KWD", "EUR", "USD"])

    def test_quoting_preserves_reference_spelling_and_multiline_bytes(self):
        reference = 'Paiement, quai "entrée"\r\n風'
        self.deposit(reference=reference)
        _, binary = self.run_export()
        self.assertTrue(binary.startswith(HEADER.encode()))
        self.assertIn(b'""entr', binary)
        self.assertTrue(binary.endswith(b"\r\n"))
        [row] = list(csv.DictReader(StringIO(binary.decode(), newline="")))
        self.assertEqual(row["reference"], reference)

    def test_open_ended_date_filters_are_inclusive(self):
        for day in (FIRST, MID, LAST):
            self.deposit(day=day)
        state = Ledger(self.path).state
        self.assertEqual([row.day for row in register.rows(state, since=MID)], [MID, LAST])
        self.assertEqual([row.day for row in register.rows(state, through=MID)], [FIRST, MID])
        reply, _ = self.run_export("day.csv", "--since", MID, "--through", MID)
        self.assertEqual(reply["exported"], 1)

    def test_account_filter_is_independent_of_dates(self):
        self.deposit(account="east", day=LAST)
        first_west = self.deposit(day=FIRST)
        self.deposit(day=MID)
        reply, binary = self.run_export("west.csv", "--account", "WeSt", "--through", FIRST)
        self.assertEqual(reply, {"exported": 1})
        [row] = list(csv.DictReader(StringIO(binary.decode(), newline="")))
        self.assertEqual((row["id"], row["account"]), (str(first_west.id), "WEST"))

    def test_empty_selection_has_only_the_header(self):
        self.deposit(day=FIRST)
        result, binary = self.run_export("empty.csv", "--since", LAST)
        self.assertEqual((result, binary), ({"exported": 0}, HEADER.encode()))

    def test_bad_filters_do_not_touch_export_or_ledger_file(self):
        path = self.root / "register.csv"
        path.write_bytes(b"previous")
        snapshot = self.path.read_bytes()
        args = ("payment", "register", str(path))
        self.cli_error("invalid date window", *args, "--since", LAST, "--through", MID)
        self.cli_error("unknown account", *args, "--account", "missing")
        self.cli_error("invalid day", *args, "--through", "2024-07-99")
        self.assertEqual(path.read_bytes(), b"previous")
        self.assertEqual(self.path.read_bytes(), snapshot)

    def test_failed_export_preserves_state_and_does_not_remove_older_register(self):
        payment = self.deposit()
        path = self.root / "register.csv"
        path.write_bytes(b"old")
        with patch.object(storage.os, "replace", side_effect=OSError), self.assertRaisesRegex(
            LedgerError, "cannot write register.csv",
        ):
            register.dump(self.ledger.state, path)
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(list(self.root.glob(".register.csv-*")), [])
        self.assertEqual(Ledger(self.path).state.payments, (payment,))

    def test_register_remains_consistent_after_archive_restore(self):
        self.deposit(amount="9.80")
        source = self.root / "exchange.json"
        self.json_cli("archive", "export", str(source))
        self.json_cli("archive", "import", str(source))
        before = self.path.read_bytes()
        result, data = self.run_export()
        self.assertEqual(result, {"exported": 2})
        [one, two] = list(csv.DictReader(StringIO(data.decode(), newline="")))
        self.assertEqual(one["id"], "1")
        self.assertEqual(two["id"], "2")
        self.assertEqual(one["reference"], two["reference"])
        self.assertEqual(self.path.read_bytes(), before)
