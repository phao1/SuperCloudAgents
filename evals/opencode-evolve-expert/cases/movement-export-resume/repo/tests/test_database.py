import sqlite3

from stockroom.catalog import Catalog
from stockroom.db import open_store, store
from stockroom.errors import StockroomError
from stockroom.inventory import Inventory
from stockroom.models import Intake

from tests.support import MONDAY, Case, seed


class DatabaseTests(Case):
    def test_each_open_enables_foreign_keys(self):
        with store(self.filename) as another:
            self.assertEqual(another.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                another.execute("INSERT INTO lot(sku,warehouse,code,quantity,received) VALUES (?,?,?,?,?)",
                                ("AX", "UNKNOWN", "L", 1, MONDAY))

    def test_memory_store_is_isolated_and_returns_named_rows(self):
        with store(":memory:") as one, store(":memory:") as two:
            seed(one)
            result = one.execute("SELECT sku FROM item ORDER BY sku").fetchone()
            self.assertEqual(result["sku"], "AX")
            self.assertEqual(Catalog(two).items(), [])
            self.assertEqual(Inventory(two).lots(), [])

    def test_double_open_preserves_data_and_internal_ids(self):
        first = self.lot("a", 8, supplier="COOP")
        with store(self.filename) as another:
            rows = Inventory(another)
            self.assertEqual(rows.lots(), [first])
            updated = rows.receive(Intake("AX", "NORTH", "A", 2, MONDAY, "COOP"))
            self.assertEqual(first.id, updated.id)
        self.assertEqual(self.inventory.lots()[0].quantity, 10)

    def test_failed_context_does_not_keep_open_handle(self):
        held = None
        with self.assertRaisesRegex(RuntimeError, "cancelled"), store(self.filename) as connection:
            held = connection
            raise RuntimeError("cancelled")
        with self.assertRaises(sqlite3.ProgrammingError):
            held.execute("SELECT 1")
        with store(self.filename) as still_valid:
            self.assertEqual(len(Catalog(still_valid).items()), 2)

    def test_unknown_store_version_is_never_overwritten(self):
        path = self.directory / "future.sqlite"
        raw = sqlite3.connect(path)
        try:
            raw.execute("PRAGMA user_version = 83")
            raw.execute("CREATE TABLE future(note TEXT)")
            raw.execute("INSERT INTO future VALUES ('preserve')")
            raw.commit()
            with self.assertRaisesRegex(StockroomError, "unsupported store version"):
                open_store(path)
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 83)
            self.assertEqual(raw.execute("SELECT * FROM future").fetchall(), [("preserve",)])
        finally:
            raw.close()
