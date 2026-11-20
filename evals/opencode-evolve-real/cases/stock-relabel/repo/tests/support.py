"""Common catalog and temporary store setup for the regression tests."""

import io
import json
import tempfile
import unittest
from pathlib import Path

from stockroom.catalog import Catalog
from stockroom.cli import run
from stockroom.db import open_store
from stockroom.inventory import Inventory
from stockroom.models import Intake
from stockroom.reporting import Reporting

MONDAY = "2024-02-26"
TUESDAY = "2024-02-27"
THURSDAY = "2024-02-29"
FRIDAY = "2024-03-01"


def seed(connection) -> None:
    catalog = Catalog(connection)
    catalog.add_warehouse("NORTH", "North Depot")
    catalog.add_warehouse("SOUTH", "South Depot")
    catalog.add_supplier("COOP", "Supply Cooperative", "orders@example.invalid")
    catalog.add_item("AX", "Axes", category="TOOLS", unit="each")
    catalog.add_item("SEED", "Fast-growing seeds", category="GARDEN", unit="box")


class Case(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.filename = self.directory / "stock.sqlite"
        self.connection = open_store(self.filename)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA synchronous = OFF")
        seed(self.connection)
        self.catalog = Catalog(self.connection)
        self.inventory = Inventory(self.connection)
        self.reporting = Reporting(self.connection)

    def lot(self, code="A", quantity=5, *, day=MONDAY, sku="AX", warehouse="NORTH",
            supplier=None, memo=""):
        return self.inventory.receive(Intake(sku, warehouse, code, quantity, day, supplier, memo))

    def file(self, name: str, content: str) -> Path:
        path = self.directory / name
        path.write_bytes(content.encode("utf-8"))
        return path

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = run(["--db", str(self.filename), *args], out, err)
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, *args: str):
        code, out, error = self.cli(*args)
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(out.endswith("\n"))
        return json.loads(out)

    def cli_error(self, message: str, *args: str) -> None:
        self.assertEqual(self.cli(*args), (2, "", f"error: {message}\n"))
