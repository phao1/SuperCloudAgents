import json

from stockroom.db import store
from stockroom.models import Intake

from tests.support import FRIDAY, MONDAY, THURSDAY, TUESDAY, Case


class WorkflowTests(Case):
    def test_import_receive_transfer_and_audit_share_the_same_inventory(self):
        catalog = self.file("catalog.json", json.dumps({
            "suppliers": [{"code": "HILL", "name": "Uphill Supplies"}],
            "items": [{"sku": "PILLOW", "name": "Shipping pillows", "category": "PACK"}],
        }))
        self.assertEqual(self.json_cli("file", "import-catalog", str(catalog)),
                         {"warehouses": 0, "suppliers": 1, "items": 1})
        receipts = self.file("receive.csv", "sku,warehouse,code,quantity,received,supplier,memo\n"
                             f"PILLOW,NORTH,A,12,{MONDAY},HILL,order 12\n"
                             f"PILLOW,NORTH,B,3,{TUESDAY},HILL,order 15\n")
        lots = self.json_cli("file", "import-receipts", str(receipts))
        self.assertEqual([row["code"] for row in lots], ["A", "B"])
        out = self.json_cli("lot", "move", "pillow", "north", "south", "14", THURSDAY)
        self.assertEqual([(row["code"], row["quantity"], row["warehouse"]) for row in out],
                         [("A", 12, "SOUTH"), ("B", 2, "SOUTH")])
        audit = self.json_cli("report", "audit", "--sku", "pillow")
        self.assertEqual([(row["code"], row["quantity"], row["recorded"]) for row in audit],
                         [("A", 0, 0), ("B", 1, 1), ("A", 12, 12), ("B", 2, 2)])
        self.assertEqual(self.json_cli("report", "audit", "--mismatches-only"), [])
        self.assertEqual([row["balance"] for row in self.json_cli("report", "as-of", MONDAY, "--sku", "pillow")], [12])

    def test_failed_close_is_visible_after_reopening_and_can_be_aborted(self):
        a = self.lot("A", 10)
        b = self.lot("B", 4)
        afternoon = self.json_cli("count", "open", "north", "Afternoon check", TUESDAY)
        identity = str(afternoon["id"])
        self.json_cli("count", "record", identity, str(a.id), "8")
        self.json_cli("count", "record", identity, str(b.id), "3")
        self.json_cli("lot", "ship", "ax", "north", "2", THURSDAY)
        self.cli_error("stock changed during count", "count", "close", identity, FRIDAY)
        [current] = self.json_cli("count", "list", "--status", "open")
        self.assertEqual(current, afternoon)
        self.assertEqual([row["observed"] for row in self.json_cli("count", "sheet", identity)], [8, 3])
        self.assertEqual(self.json_cli("count", "abort", identity, FRIDAY)["status"], "aborted")
        self.assertEqual([row["quantity"] for row in self.json_cli("lot", "list")], [8, 4])
        self.assertEqual(self.json_cli("report", "activity", "--kind", "adjust"), [])

    def test_receipts_and_count_adjustments_preserve_catalog_and_history(self):
        self.inventory.receive_all([
            Intake("AX", "NORTH", "FIRST", 9, MONDAY, memo="intake slip"),
            Intake("AX", "NORTH", "SECOND", 2, TUESDAY, memo="intake slip"),
            Intake("AX", "SOUTH", "THIRD", 4, TUESDAY, memo="intake slip"),
        ])
        start = self.json_cli("count", "open", "north", "Spot check", THURSDAY)
        identity = str(start["id"])
        sheet = self.json_cli("count", "sheet", identity)
        self.json_cli("count", "record", identity, str(sheet[0]["lot_id"]), "9")
        self.json_cli("count", "record", identity, str(sheet[1]["lot_id"]), "0")
        self.assertEqual(self.json_cli("count", "close", identity, FRIDAY)["status"], "closed")
        report = self.json_cli("report", "trend", "--period", "week", "--warehouse", "north")
        self.assertEqual(report, [{"period": MONDAY, "sku": "AX", "warehouse": "NORTH",
                                   "received": 11, "shipped": 0, "adjusted": -2, "transferred": 0,
                                   "net": 9, "events": 3}])
        self.assertEqual(self.json_cli("item", "show", "ax")["name"], "Axes")
        with store(self.filename) as fresh:
            self.assertEqual(fresh.execute("SELECT COUNT(*) FROM lot").fetchone()[0], 3)

    def test_planning_uses_live_data_without_reserving_a_lot(self):
        self.lot("A", 7, warehouse="NORTH")
        self.lot("B", 8, warehouse="SOUTH")
        order = self.file("order.json", '[{"sku":"AX","quantity":6},{"sku":"ax","quantity":3}]')
        plan = self.json_cli("order", "plan", str(order), "--warehouse", "south")
        self.assertEqual(plan["shortages"], [{"sku": "AX", "missing": 1}])
        self.assertEqual([row["quantity"] for row in plan["picks"]], [8])
        self.json_cli("lot", "move", "AX", "NORTH", "SOUTH", "4", TUESDAY)
        plan = self.json_cli("order", "plan", str(order), "--warehouse", "south")
        self.assertEqual(plan["shortages"], [])
        self.assertEqual([(row["lot"], row["quantity"]) for row in plan["picks"]], [("B", 8), ("A", 1)])
        totals = self.json_cli("report", "balances", "--warehouse", "south")
        self.assertEqual([(row["quantity"], row["positive_lots"]) for row in totals], [(12, 2)])

    def test_depleted_lot_notes_survive_rereceipt_and_remain_ordered(self):
        lot = self.lot("LAST", 2)
        a = self.json_cli("note", "add", str(lot.id), MONDAY, "before shipment")
        self.json_cli("lot", "ship", "AX", "NORTH", "2", TUESDAY)
        self.assertEqual(self.json_cli("lot", "list"), [])
        reopened = self.json_cli("lot", "receive", "AX", "NORTH", "LAST", "3", MONDAY)
        self.assertEqual((reopened["id"], reopened["quantity"]), (lot.id, 3))
        b = self.json_cli("note", "add", str(lot.id), THURSDAY, "after receipt")
        self.assertEqual(self.json_cli("note", "list", str(lot.id)), [a, b])
        self.assertEqual(self.json_cli("report", "audit")[0]["recorded"], 3)

    def test_archiving_a_warehouse_hides_it_from_planning_but_not_reports(self):
        self.lot("A", 7, warehouse="NORTH")
        self.lot("B", 4, warehouse="SOUTH")
        request = self.file("order.json", '[{"sku":"AX","quantity":11}]')
        self.json_cli("warehouse", "status", "SOUTH", "inactive")
        plan = self.json_cli("order", "plan", str(request))
        self.assertEqual(plan["shortages"], [{"sku": "AX", "missing": 4}])
        self.assertEqual(self.json_cli("report", "balances", "--warehouse", "south"), [
            {"sku": "AX", "warehouse": "SOUTH", "quantity": 4, "positive_lots": 1},
        ])
        self.assertEqual(len(self.json_cli("report", "as-of", MONDAY, "--warehouse", "south")), 1)
        self.cli_error("inactive warehouse", "lot", "move", "AX", "NORTH", "SOUTH", "1", TUESDAY)
        self.assertEqual(self.json_cli("report", "activity", "--kind", "transfer"), [])

    def test_movement_date_filters_agree_with_a_backdated_stock_audit(self):
        lot = self.lot("B", 9, day=THURSDAY)
        self.json_cli("lot", "adjust", "AX", "NORTH", "B", "-3", TUESDAY)
        earlier = self.json_cli("report", "activity", "--since", MONDAY, "--through", TUESDAY)
        self.assertEqual([(row["kind"], row["delta"]) for row in earlier], [("adjust", -3)])
        [as_of] = self.json_cli("report", "as-of", TUESDAY)
        self.assertEqual((as_of["lot_id"], as_of["balance"], as_of["latest"]), (lot.id, -3, TUESDAY))
        [audit] = self.json_cli("report", "audit", "--warehouse", "north")
        self.assertEqual((audit["lot_id"], audit["quantity"], audit["recorded"]), (lot.id, 6, 6))
