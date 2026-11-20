from stockroom.errors import Conflict, Missing, StockroomError
from stockroom.models import Item, Supplier, Warehouse

from tests.support import Case


class CatalogTests(Case):
    def test_add_item_normalizes_codes_and_preserves_unicode(self):
        added = self.catalog.add_item("Sp-2", "  Ångström 场地  ", category="Storage", unit=" set ")
        self.assertEqual(added, Item("SP-2", "Ångström 场地", "STORAGE", "set"))
        self.assertEqual(self.catalog.item("sp-2"), added)
        self.assertEqual(self.catalog.items("storage"), [added])

    def test_add_warehouse_validates_code(self):
        added = self.catalog.add_warehouse("shelf-9", "Adjacent storage")
        self.assertEqual(added, Warehouse("SHELF-9", "Adjacent storage"))
        with self.assertRaisesRegex(StockroomError, "invalid warehouse"):
            self.catalog.add_warehouse("shelf 10", "Storage")

    def test_add_supplier_defaults_contact_and_trims_name(self):
        added = self.catalog.add_supplier("dg", "  South Division ")
        self.assertEqual(added, Supplier("DG", "South Division", ""))
        self.assertIn(added, self.catalog.suppliers())

    def test_distinct_entities_have_independent_code_namespaces(self):
        self.catalog.add_item("same", "Rope")
        self.catalog.add_warehouse("same", "Warehouse")
        self.catalog.add_supplier("same", "Vendor")
        self.assertEqual(self.catalog.item("SAME").name, "Rope")
        self.assertEqual(self.catalog.rows.warehouse("SAME").name, "Warehouse")
        self.assertEqual(self.catalog.rows.supplier("SAME").name, "Vendor")

    def test_duplicate_catalog_entries_do_not_replace_existing(self):
        with self.assertRaisesRegex(Conflict, "sku already exists"):
            self.catalog.add_item("ax", "Replacement")
        with self.assertRaisesRegex(Conflict, "warehouse already exists"):
            self.catalog.add_warehouse("north", "Replacement")
        with self.assertRaisesRegex(Conflict, "supplier already exists"):
            self.catalog.add_supplier("coop", "Replacement")
        self.assertEqual(self.catalog.item("ax").name, "Axes")
        self.assertEqual(self.catalog.rows.warehouse("NORTH").name, "North Depot")
        self.assertEqual(self.catalog.rows.supplier("COOP").name, "Supply Cooperative")

    def test_item_status_filter_by_category_and_active(self):
        self.catalog.add_item("A0", "Rake", category="TOOLS")
        self.assertFalse(self.catalog.item_status("ax", False).active)
        self.assertEqual([row.sku for row in self.catalog.items("tools")], ["A0"])
        self.assertEqual([row.sku for row in self.catalog.items("tools", True)], ["A0", "AX"])
        self.assertTrue(self.catalog.item_status("ax", True).active)
        self.assertEqual([row.sku for row in self.catalog.items()], ["A0", "AX", "SEED"])

    def test_warehouse_status_and_order(self):
        self.catalog.add_warehouse("AR", "Offsite storage")
        self.assertFalse(self.catalog.warehouse_status("north", False).active)
        self.assertEqual([row.code for row in self.catalog.warehouses()], ["AR", "SOUTH"])
        self.assertEqual([row.code for row in self.catalog.warehouses(True)], ["AR", "NORTH", "SOUTH"])

    def test_supplier_status_and_order(self):
        self.catalog.add_supplier("A1", "Offsite supplier", "  fallback  ")
        self.assertFalse(self.catalog.supplier_status("coop", False).active)
        self.assertEqual([row.code for row in self.catalog.suppliers()], ["A1"])
        self.assertEqual([row.contact for row in self.catalog.suppliers(True)],
                         ["  fallback  ", "orders@example.invalid"])

    def test_missing_show_and_status(self):
        for method, args, message in [
            (self.catalog.item, ("MISSING",), "unknown sku"),
            (self.catalog.item_status, ("MISSING", True), "unknown sku"),
            (self.catalog.warehouse_status, ("MISSING", True), "unknown warehouse"),
            (self.catalog.supplier_status, ("MISSING", True), "unknown supplier"),
        ]:
            with self.subTest(method=method.__name__), self.assertRaisesRegex(Missing, message):
                method(*args)

    def test_invalid_catalog_title_or_boolean(self):
        with self.assertRaisesRegex(StockroomError, "invalid name"):
            self.catalog.add_item("LAB", "  ")
        with self.assertRaisesRegex(StockroomError, "invalid unit"):
            self.catalog.add_item("LAB", "Tools", unit="")
        with self.assertRaisesRegex(StockroomError, "invalid active flag"):
            self.catalog.item_status("AX", 0)
        with self.assertRaises(Missing):
            self.catalog.item("LAB")
        self.assertTrue(self.catalog.item("AX").active)
