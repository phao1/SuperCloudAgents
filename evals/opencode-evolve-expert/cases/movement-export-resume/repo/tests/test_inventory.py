from stockroom.errors import Conflict, Missing, StockroomError
from stockroom.models import Intake

from tests.support import FRIDAY, MONDAY, THURSDAY, TUESDAY, Case


class InventoryTests(Case):
    def test_receive_records_source_and_movement(self):
        sample = self.lot("S", 3, supplier="Coop", memo="unloaded in bay 2")
        self.assertEqual(sample.quantity, 3)
        self.assertEqual(sample.supplier, "COOP")
        [movement] = self.reporting.activity()
        self.assertEqual((movement.sku, movement.warehouse, movement.lot), ("AX", "NORTH", "S"))
        self.assertEqual((movement.kind, movement.delta, movement.day), ("receive", 3, MONDAY))
        self.assertEqual(movement.memo, "unloaded in bay 2")

    def test_receive_same_lot_accumulates_and_preserves_id(self):
        original = self.lot("rerun", 3, supplier="Coop")
        updated = self.lot("RERUN", 2, supplier="COOP")
        self.assertEqual(updated.id, original.id)
        self.assertEqual((updated.code, updated.quantity), ("RERUN", 5))
        self.assertEqual([record.delta for record in self.reporting.activity()], [3, 2])

    def test_receive_metadata_conflicts_are_atomic(self):
        self.lot("X", 5, supplier="Coop")
        for changes in ({"day": TUESDAY, "supplier": "Coop"}, {"day": MONDAY, "supplier": None}):
            with self.subTest(changes=changes), self.assertRaisesRegex(Conflict, "^lot details differ$"):
                self.lot("X", 2, **changes)
        [unchanged] = self.inventory.lots()
        self.assertEqual(unchanged.quantity, 5)
        self.assertEqual(len(self.reporting.activity()), 1)

    def test_missing_catalog_values(self):
        for row, message in [
            (Intake("NONE", "NORTH", "A", 2, MONDAY), "unknown sku"),
            (Intake("AX", "NOWHERE", "A", 2, MONDAY), "unknown warehouse"),
            (Intake("AX", "NORTH", "A", 2, MONDAY, "LOST"), "unknown supplier"),
        ]:
            with self.subTest(row=row), self.assertRaisesRegex(Missing, message):
                self.inventory.receive(row)
        self.assertEqual(self.inventory.lots(), [])
        self.assertEqual(self.reporting.activity(), [])

    def test_inactive_catalog_values_cannot_receive(self):
        self.catalog.item_status("AX", False)
        self.catalog.warehouse_status("NORTH", False)
        self.catalog.supplier_status("COOP", False)
        with self.assertRaisesRegex(StockroomError, "inactive item"):
            self.lot(supplier="COOP")
        self.catalog.item_status("AX", True)
        with self.assertRaisesRegex(StockroomError, "inactive warehouse"):
            self.lot(supplier="COOP")
        self.catalog.warehouse_status("NORTH", True)
        with self.assertRaisesRegex(StockroomError, "inactive supplier"):
            self.lot(supplier="COOP")
        self.assertEqual(self.inventory.lots(), [])

    def test_bulk_receipt_rolls_back_on_second_unknown_item(self):
        batch = [Intake("AX", "NORTH", "A", 5, MONDAY), Intake("MISSING", "NORTH", "B", 5, MONDAY)]
        with self.assertRaises(Missing):
            self.inventory.receive_all(iter(batch))
        self.assertEqual(self.inventory.lots(), [])
        self.assertEqual(self.reporting.activity(), [])
        self.assertEqual(self.inventory.receive_all([]), [])

    def test_bulk_receipt_shares_transaction_and_keeps_input_order(self):
        batch = [Intake("AX", "NORTH", "A", 5, MONDAY), Intake("AX", "NORTH", "a", 3, MONDAY),
                 Intake("SEED", "NORTH", "a", 4, MONDAY)]
        rows = self.inventory.receive_all(iter(batch))
        self.assertEqual([row.quantity for row in rows], [5, 8, 4])
        self.assertEqual(rows[0].id, rows[1].id)
        self.assertNotEqual(rows[0].id, rows[2].id)

    def test_adjustment_to_zero_is_visible_with_the_flag(self):
        initial = self.lot(quantity=5)
        result = self.inventory.adjust("ax", "north", "a", -5, TUESDAY)
        self.assertEqual(result.id, initial.id)
        self.assertEqual(result.quantity, 0)
        self.assertEqual(self.inventory.lots(), [])
        self.assertEqual(self.inventory.lots(include_empty=True), [result])
        self.assertEqual(self.reporting.activity(kind="adjust")[0].delta, -5)

    def test_adjustment_rejects_negative_end_and_keeps_audit_clean(self):
        self.lot(quantity=3)
        with self.assertRaisesRegex(StockroomError, "insufficient stock"):
            self.inventory.adjust("AX", "NORTH", "A", -4, TUESDAY)
        self.assertEqual(self.inventory.lots()[0].quantity, 3)
        self.assertEqual(self.reporting.activity(kind="adjust"), [])

    def test_adjust_does_not_require_an_active_catalog_item(self):
        self.lot(quantity=3)
        self.catalog.item_status("AX", False)
        self.assertEqual(self.inventory.adjust("AX", "NORTH", "A", 2, TUESDAY).quantity, 5)
        with self.assertRaisesRegex(Missing, "unknown lot"):
            self.inventory.adjust("AX", "NORTH", "MISSING", 1, TUESDAY)

    def test_ship_uses_received_then_creation_order(self):
        recent = self.lot("RECENT", 5, day=THURSDAY)
        first = self.lot("Y", 2, day=TUESDAY)
        second = self.lot("X", 4, day=TUESDAY)
        shipped = self.inventory.ship("ax", "north", 3, FRIDAY, "dock loading")
        self.assertEqual([(lot.id, lot.quantity) for lot in shipped], [(first.id, 0), (second.id, 3)])
        self.assertEqual(self.inventory.rows.by_id(recent.id).quantity, 5)
        self.assertEqual([(event.lot, event.delta) for event in self.reporting.activity(kind="ship")],
                         [("Y", -2), ("X", -1)])

    def test_shipping_insufficient_stock_preserves_unapplied_plan(self):
        self.lot("A", 1)
        self.lot("B", 1)
        with self.assertRaisesRegex(StockroomError, "insufficient stock"):
            self.inventory.ship("AX", "NORTH", 3, FRIDAY)
        self.assertEqual([row.quantity for row in self.inventory.lots()], [1, 1])
        self.assertEqual(self.reporting.activity(kind="ship"), [])

    def test_move_preserves_intake_details_and_uses_paired_events(self):
        source = self.lot("A", 5, supplier="COOP", memo="source")
        [arrived] = self.inventory.move("AX", "NORTH", "SOUTH", 4, THURSDAY, "route 1")
        self.assertEqual((arrived.received, arrived.supplier, arrived.quantity), (MONDAY, "COOP", 4))
        self.assertNotEqual(arrived.id, source.id)
        self.assertEqual(self.inventory.rows.by_id(source.id).quantity, 1)
        events = self.reporting.activity(kind="transfer")
        self.assertEqual([(event.warehouse, event.counterpart, event.delta, event.memo) for event in events],
                         [("NORTH", "SOUTH", -4, "route 1"), ("SOUTH", "NORTH", 4, "route 1")])

    def test_move_rolls_back_earlier_rows_on_destination_conflict(self):
        self.lot("A", 3)
        self.lot("B", 4, day=TUESDAY)
        self.lot("B", 2, day=THURSDAY, warehouse="SOUTH")
        before = self.inventory.lots()
        with self.assertRaisesRegex(Conflict, "lot details differ"):
            self.inventory.move("AX", "NORTH", "SOUTH", 4, FRIDAY)
        self.assertEqual(self.inventory.lots(), before)
        self.assertEqual(self.reporting.activity(kind="transfer"), [])

    def test_same_and_inactive_destination_are_rejected(self):
        self.lot()
        with self.assertRaisesRegex(StockroomError, "same warehouse"):
            self.inventory.move("AX", "NORTH", "north", 1, TUESDAY)
        self.catalog.warehouse_status("SOUTH", False)
        with self.assertRaisesRegex(StockroomError, "inactive warehouse"):
            self.inventory.move("AX", "NORTH", "SOUTH", 1, TUESDAY)
        self.assertEqual(self.reporting.activity(kind="transfer"), [])

    def test_lot_filters_combine_warehouse_sku_and_empty(self):
        self.lot("X", warehouse="SOUTH")
        self.lot("X", sku="SEED")
        self.lot("X")
        self.inventory.adjust("ax", "north", "x", -5, TUESDAY)
        self.assertEqual(self.inventory.lots("ax", "north"), [])
        self.assertEqual([row.code for row in self.inventory.lots("ax", "north", True)], ["X"])
        self.assertEqual([row.sku for row in self.inventory.lots(warehouse="north")], ["SEED"])
        self.assertEqual([row.warehouse for row in self.inventory.lots(sku="AX")], ["SOUTH"])
