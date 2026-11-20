import json

from stockroom.errors import Conflict, StockroomError
from stockroom.files import csv_requests, export_catalog, import_catalog, import_receipts

from tests.support import MONDAY, TUESDAY, Case

HEADLINE = "sku,warehouse,code,quantity,received,supplier,memo\n"


class ExchangeTests(Case):
    def test_catalog_import_is_durable_and_resolves_defaults(self):
        reply = import_catalog(self.connection, self.file("new.json", json.dumps({
            "warehouses": [{"code": "remote", "name": "Remote storage"}],
            "suppliers": [{"code": "hand", "name": "Handmade Co."}],
            "items": [{"sku": "rope", "name": "Rope 🧵"}],
        })))
        self.assertEqual(reply, {"warehouses": 1, "suppliers": 1, "items": 1})
        self.assertEqual(self.json_cli("item", "show", "rope")["name"], "Rope 🧵")
        self.assertEqual(self.catalog.item("ROPE").category, "GENERAL")
        self.assertEqual(self.catalog.rows.supplier("HAND").contact, "")
        self.assertEqual(self.catalog.rows.warehouse("REMOTE").name, "Remote storage")

    def test_catalog_import_rolls_back_at_the_first_write_conflict(self):
        path = self.file("bad.json", json.dumps({
            "warehouses": [{"code": "NEW", "name": "New storage"}],
            "items": [{"sku": "ax", "name": "Conflicting item"}],
        }))
        with self.assertRaisesRegex(Conflict, "sku already exists"):
            import_catalog(self.connection, path)
        self.assertEqual([row.code for row in self.catalog.warehouses()], ["NORTH", "SOUTH"])
        self.assertEqual(self.catalog.item("AX").name, "Axes")

    def test_catalog_import_rejects_wrong_outer_shapes(self):
        for text, message in [
            ("{", "invalid catalog file"), ("null", "invalid catalog fields"),
            ('{"items":{} }', "invalid catalog groups"), ('{"extra":[]}', "invalid catalog fields"),
            ('{"warehouses":[12]}', "invalid catalog groups"), ('{"items":[{"sku":"UNNAMED"}]}', "invalid catalog record"),
        ]:
            with self.subTest(text=text), self.assertRaisesRegex(StockroomError, message):
                import_catalog(self.connection, self.file("bad.json", text))
        self.assertEqual([row.sku for row in self.catalog.items()], ["AX", "SEED"])

    def test_catalog_accepts_an_empty_exchange(self):
        path = self.file("empty.json", '{"warehouses":[],"items":[]}')
        self.assertEqual(import_catalog(self.connection, path), {"warehouses": 0, "suppliers": 0, "items": 0})
        self.assertEqual(len(self.catalog.warehouses()), 2)

    def test_catalog_export_filters_each_catalog_group(self):
        self.catalog.item_status("AX", False)
        self.catalog.warehouse_status("NORTH", False)
        self.catalog.supplier_status("COOP", False)
        active = export_catalog(self.connection)
        self.assertTrue(active.endswith("\n"))
        self.assertEqual([record["sku"] for record in json.loads(active)["items"]], ["SEED"])
        self.assertEqual([record["code"] for record in json.loads(active)["warehouses"]], ["SOUTH"])
        self.assertEqual(json.loads(active)["suppliers"], [])
        complete = json.loads(export_catalog(self.connection, True))
        self.assertEqual([record["active"] for record in complete["items"]], [False, True])
        self.assertEqual([record["active"] for record in complete["suppliers"]], [False])

    def test_csv_receipt_preserves_commas_unicode_and_line_breaks(self):
        path = self.file("receive.csv", HEADLINE +
                         f'AX,NORTH,X,7,{MONDAY},COOP,"unload,then check\n雪"\n'
                         f'AX,SOUTH,X,2,{TUESDAY},,\n')
        output = import_receipts(self.connection, path)
        self.assertEqual([(row.code, row.quantity, row.supplier) for row in output],
                         [("X", 7, "COOP"), ("X", 2, None)])
        self.assertEqual([entry.memo for entry in self.reporting.activity()], ["unload,then check\n雪", ""])

    def test_csv_receipts_accepts_header_order_and_optional_fields(self):
        path = self.file("receive.csv", f"received,quantity,code,warehouse,sku\r\n{MONDAY},9,L,NORTH,AX\r\n")
        [result] = import_receipts(self.connection, path)
        self.assertEqual((result.code, result.received, result.quantity, result.supplier), ("L", MONDAY, 9, None))
        self.assertEqual(self.reporting.activity()[0].memo, "")

    def test_csv_bad_headers_do_not_touch_data(self):
        for header in ("", "sku,received\n", HEADLINE.rstrip("\n") + ",memo\n",
                       HEADLINE.rstrip("\n") + ",extra\n"):
            with self.subTest(header=header), self.assertRaisesRegex(StockroomError, "invalid receipt columns"):
                import_receipts(self.connection, self.file("invalid.csv", header))
        self.assertEqual(self.inventory.lots(), [])

    def test_csv_row_errors_report_physical_line(self):
        bad = [HEADLINE, f'AX,NORTH,A,4,{MONDAY},,"multi\nline"\n', f"AX,NORTH,B,absent,{TUESDAY},,\n"]
        with self.assertRaisesRegex(StockroomError, "invalid receipt line 4"):
            csv_requests("".join(bad).splitlines(keepends=True))
        bad = [HEADLINE, f"AX,NORTH,A,4,{MONDAY},,,extra\n"]
        with self.assertRaisesRegex(StockroomError, "invalid receipt line 2"):
            csv_requests("".join(bad).splitlines(keepends=True))

    def test_csv_conflict_does_not_leave_a_partially_received_batch(self):
        initial = self.lot("X", 3)
        source = HEADLINE + f"AX,NORTH,Y,9,{MONDAY},,\nAX,NORTH,X,3,{TUESDAY},,\n"
        with self.assertRaisesRegex(Conflict, "lot details differ"):
            import_receipts(self.connection, self.file("conflict.csv", source))
        self.assertEqual(self.inventory.lots(), [initial])
        self.assertEqual(len(self.reporting.activity()), 1)
