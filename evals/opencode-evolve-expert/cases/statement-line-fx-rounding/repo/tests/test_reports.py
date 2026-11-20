import csv
from decimal import Decimal
from io import StringIO

from quayside import rates, statements
from quayside.errors import LedgerError
from quayside.ledger import Ledger
from quayside.model import Line

from tests.support import FIRST, LAST, MID, BookTest


class RatesTests(BookTest):
    def test_quote_uses_latest_rate_on_or_before_the_posted_day(self):
        self.rate(day=MID, amount="0.75")
        self.rate(day=FIRST, amount="0.50")
        self.rate(day=LAST, amount="0.90")
        self.assertEqual(rates.effective(self.ledger.state, "usd", "eur", "2024-07-14"), Decimal("0.5"))
        self.assertEqual(rates.effective(self.ledger.state, "usd", "eur", MID), Decimal("0.75"))
        self.assertEqual(rates.effective(self.ledger.state, "usd", "eur", LAST), Decimal("0.9"))

    def test_quote_rounds_negative_and_positive_ties_away_from_zero(self):
        self.rate()
        for input, expected in (("0.05", "0.03"), ("-0.05", "-0.03"), ("2.50", "1.25")):
            with self.subTest(input=input):
                self.assertEqual(self.json_cli("rate", "quote", "usd", "eur", MID, input),
                                 {"currency": "EUR", "amount": expected})

    def test_currency_pair_has_no_inferred_inverse(self):
        self.rate()
        self.cli_error("missing rate EUR/USD for " + MID, "rate", "quote", "EUR", "USD", MID, "1.00")
        self.assertEqual(self.json_cli("rate", "quote", "JPY", "JPY", MID, "70")["amount"], "70")

    def test_replacing_quote_preserves_other_days_and_pairs(self):
        self.rate()
        distinct = self.rate(source="JPY", amount="0.045", day=FIRST)
        later = self.rate(day=MID, amount="0.5")
        updated = self.rate(day=FIRST, amount="0.7")
        self.assertEqual(set(Ledger(self.path).state.rates), {distinct, later, updated})

    def test_invalid_rate_does_not_replace_prior_data(self):
        quote = self.rate()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(LedgerError, "invalid rate"):
            self.rate(amount="0")
        with self.assertRaisesRegex(LedgerError, "invalid rate"):
            self.rate(target="USD", amount="1.1")
        self.assertEqual(Ledger(self.path).state.rates, (quote,))
        self.assertEqual(self.path.read_bytes(), before)


class StatementTests(BookTest):
    def test_native_statements_group_totals_and_keep_fixed_wire_scale(self):
        self.bill()
        self.bill(account="yen", reference="bent roof", lines=[Line("Nails", 3, "125")], day=MID)
        self.deposit(account="yen", amount="300", day=LAST)
        self.deposit(amount="4.20")
        self.deposit(account="kw", amount="-0.001", day=LAST)
        state = Ledger(self.path).state
        report = statements.project(state, FIRST, LAST)
        self.assertEqual([(row.day, row.kind, row.amount) for row in report.entries], [
            (FIRST, "invoice", "6.00"), (FIRST, "payment", "-4.20"),
            (MID, "invoice", "375"), (LAST, "payment", "-300"), (LAST, "payment", "0.001"),
        ])
        self.assertEqual(report.totals, {"JPY": "75", "KWD": "0.001", "USD": "1.80"})

    def test_account_and_date_filters_happen_before_rate_lookup(self):
        self.bill()
        self.bill(account="kw", reference="kw bill", day=LAST)
        self.rate()
        self.assertEqual(self.json_cli("statement", "open", FIRST, LAST, "--account", "west",
                                       "--currency", "EUR")["totals"], {"EUR": "3.00"})
        self.assertEqual(self.json_cli("statement", "open", FIRST, MID,
                                       "--currency", "eur")["totals"], {"EUR": "3.00"})
        self.cli_error("missing rate KWD/EUR for " + LAST, "statement", "open",
                       FIRST, LAST, "--currency", "EUR")

    def test_exchange_is_based_on_each_entry_date(self):
        self.bill(reference="first")
        self.bill(reference="second", day=MID)
        self.rate()
        self.rate(day=MID, amount="0.75")
        self.rate(day=LAST, amount="0.10")
        rows = self.json_cli("statement", "open", FIRST, LAST, "--currency", "EUR")
        self.assertEqual([row["amount"] for row in rows["entries"]], ["3.00", "4.50"])
        self.assertEqual(rows["totals"], {"EUR": "7.50"})

    def test_csv_matches_json_entry_order_and_escapes_display_labels(self):
        self.bill(reference=' Dock "III", stage\n二 ')
        self.deposit(reference="Bank", day=MID)
        code, out, error = self.cli("statement", "open", FIRST, LAST, "--format", "csv")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(out.endswith("\r\n"))
        transport = list(csv.DictReader(StringIO(out, newline="")))
        entries = self.json_cli("statement", "open", FIRST, LAST)["entries"]
        self.assertEqual(transport, [{k: str(v) for k, v in row.items()} for row in entries])

    def test_numeric_id_order_for_same_day_and_kind(self):
        for number in range(12):
            self.bill(reference=f"Box {number}")
        rows = self.json_cli("statement", "open", FIRST, FIRST)["entries"]
        self.assertEqual([row["id"] for row in rows], list(range(1, 13)))

    def test_no_target_report_is_read_only_with_empty_window(self):
        self.bill()
        old = self.path.read_bytes()
        self.assertEqual(self.json_cli("statement", "open", MID, MID), {"entries": [], "totals": {}})
        self.cli_error("invalid date window", "statement", "open", LAST, FIRST)
        self.assertEqual(self.path.read_bytes(), old)
