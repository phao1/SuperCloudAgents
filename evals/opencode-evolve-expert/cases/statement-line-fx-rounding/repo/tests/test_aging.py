from quayside import aging
from quayside.ledger import Ledger
from quayside.model import Line

from tests.support import FIRST, LAST, MID, BookTest


class AgingTests(BookTest):
    def test_prior_deposits_cover_oldest_postings_even_if_entered_later(self):
        newer = self.bill(reference="new", day=MID)
        self.bill(reference="old", day=FIRST)
        self.deposit(amount="8.75", day=LAST)
        before = self.path.read_bytes()
        result = aging.report(Ledger(self.path).state, LAST, "west")
        self.assertEqual([(row.invoice, row.remaining, row.age_days) for row in result.rows],
                         [(newer.id, "3.25", 16)])
        self.assertEqual(result.unallocated, {"WEST": "0.00"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_reports_as_of_requested_day_exclude_future_bills_and_deposits(self):
        bill = self.bill()
        self.bill(reference="new", day=LAST)
        self.deposit(amount="6.00", day=LAST)
        [row] = self.json_cli("statement", "aging", MID, "--account", "west")["rows"]
        self.assertEqual((row["invoice"], row["remaining"], row["age_days"]), (bill.id, "6.00", 14))

    def test_refund_reduces_cash_without_making_invoice_balance_larger(self):
        self.bill()
        self.deposit(amount="-0.30")
        report = self.json_cli("statement", "aging", FIRST, "--account", "west")
        self.assertEqual(report["rows"][0]["remaining"], "6.00")
        self.assertEqual(report["unallocated"], {"WEST": "-0.30"})

    def test_excess_deposit_remains_in_original_currency(self):
        self.bill(account="kw", lines=[Line("Rent", 1, "2.500")])
        self.deposit(account="kw", amount="3.000")
        report = self.json_cli("statement", "aging", FIRST)
        self.assertEqual(report, {"rows": [], "unallocated": {
            "EAST": "0.00", "KW": "0.500", "WEST": "0.00", "YEN": "0",
        }})

    def test_tie_goes_to_earlier_issued_id(self):
        self.bill(reference="second alphabetically")
        latest = self.bill(reference="first alphabetically")
        self.deposit(amount="6")
        result = aging.report(self.ledger.state, FIRST, "west")
        self.assertEqual([row.invoice for row in result.rows], [latest.id])

    def test_empty_preview_still_validates_account_and_date(self):
        self.cli_error("invalid day", "statement", "aging", "2024-7-01")
        self.cli_error("unknown account", "statement", "aging", FIRST, "--account", "silent")
        self.assertEqual(self.json_cli("statement", "aging", FIRST, "--account", "yen"),
                         {"rows": [], "unallocated": {"YEN": "0"}})
