import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from quayside import accounts, invoices, payments, rates
from quayside.cli import run
from quayside.ledger import Ledger
from quayside.model import Line

FIRST = "2024-07-01"
MID = "2024-07-15"
LAST = "2024-07-31"


def fixture(path):
    ledger = Ledger(path)
    for code, name, currency in (("west", "West", "USD"), ("east", "East", "EUR"),
                                 ("yen", "Yen desk", "JPY"), ("kw", "Kuwait", "KWD")):
        ledger.change(accounts.put, code, name, currency)
    return ledger


class BookTest(TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = self.root / "ledger.json"
        self.ledger = fixture(self.path)

    def bill(self, account="west", reference="Ticket", day=FIRST, lines=None):
        if lines is None:
            lines = [Line("Packing", 2, "3.00")]
        return self.ledger.change(invoices.issue, account, reference, day, lines)

    def deposit(self, account="west", amount="2.10", day=FIRST, reference="bank"):
        return self.ledger.change(payments.add, account, day, amount, reference)

    def rate(self, source="USD", target="EUR", day=FIRST, amount="0.50"):
        return self.ledger.change(rates.put, source, target, day, amount)

    def file(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = run(["--ledger", str(self.path), *args], out, err)
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, *args: str):
        code, out, err = self.cli(*args)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("\n"))
        return json.loads(out)

    def cli_error(self, error: str, *args: str):
        self.assertEqual(self.cli(*args), (2, "", f"error: {error}\n"))
