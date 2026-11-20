from stockroom.errors import Missing, StockroomError
from stockroom.fulfillment import Demand, Fulfillment, Pick, PickList, Shortage, load_requests

from tests.support import MONDAY, TUESDAY, Case


class FulfillmentTests(Case):
    def setUp(self):
        super().setUp()
        self.planner = Fulfillment(self.connection)
        self.later = self.lot("R", 6, day=TUESDAY)
        self.earlier = self.lot("Q", 2, day=MONDAY)
        self.south = self.lot("X", 9, warehouse="SOUTH")
        self.seeds = self.lot("R", 3, sku="SEED")

    def test_default_order_is_by_warehouse_then_fifo(self):
        decisions = self.planner.plan([Demand("ax", 10)])
        self.assertEqual(decisions, PickList([
            Pick("AX", "NORTH", self.earlier.code, self.earlier.id, 2),
            Pick("AX", "NORTH", self.later.code, self.later.id, 6),
            Pick("AX", "SOUTH", self.south.code, self.south.id, 2),
        ], []))

    def test_explicit_warehouses_change_order_and_limit_scope(self):
        decisions = self.planner.plan([Demand("AX", 13)], ["south", "north"])
        self.assertEqual([(pick.lot_id, pick.quantity) for pick in decisions.picks],
                         [(self.south.id, 9), (self.earlier.id, 2), (self.later.id, 2)])
        decisions = self.planner.plan([Demand("AX", 12)], ["SOUTH"])
        self.assertEqual(decisions.shortages, [Shortage("AX", 3)])
        self.assertEqual(self.planner.plan([Demand("AX", 12)], []), PickList([], [Shortage("AX", 12)]))

    def test_combined_requests_preserve_first_normalized_item_order(self):
        decisions = self.planner.plan(iter([Demand("seed", 2), Demand("ax", 1), Demand("SEED", 6)]))
        self.assertEqual(decisions.shortages, [Shortage("SEED", 5)])
        self.assertEqual([(pick.sku, pick.quantity) for pick in decisions.picks], [("SEED", 3), ("AX", 1)])

    def test_unknown_and_inactive_warehouses_are_distinct(self):
        self.catalog.warehouse_status("SOUTH", False)
        with self.assertRaisesRegex(Missing, "unknown warehouse"):
            self.planner.plan([], ["missing"])
        with self.assertRaisesRegex(StockroomError, "inactive warehouse"):
            self.planner.plan([], ["south"])
        with self.assertRaisesRegex(StockroomError, "duplicate warehouse"):
            self.planner.plan([], ["north", "North"])
        decisions = self.planner.plan([Demand("AX", 9)])
        self.assertEqual(decisions.shortages, [Shortage("AX", 1)])

    def test_missing_and_inactive_items_error_even_on_no_sites(self):
        self.catalog.item_status("AX", False)
        with self.assertRaisesRegex(Missing, "unknown sku"):
            self.planner.plan([Demand("none", 4)], [])
        with self.assertRaisesRegex(StockroomError, "inactive item"):
            self.planner.plan([Demand("ax", 4)], [])
        with self.assertRaisesRegex(StockroomError, "invalid quantity"):
            self.planner.plan([Demand("seed", False)], [])

    def test_plan_does_not_modify_lots_or_movement_history(self):
        before, trail = self.inventory.lots(), self.reporting.activity()
        orders = [Demand("AX", 20), Demand("SEED", 14)]
        result = self.planner.plan(orders)
        self.assertEqual(result.shortages, [Shortage("AX", 3), Shortage("SEED", 11)])
        self.assertEqual(self.inventory.lots(), before)
        self.assertEqual(self.reporting.activity(), trail)
        self.assertEqual(self.planner.plan(orders), result)

    def test_order_json_command_uses_selected_sites(self):
        path = self.file("order.json", '[{"sku":"ax","quantity":14}]')
        result = self.json_cli("order", "plan", str(path), "--warehouse", "south")
        self.assertEqual(result, {
            "picks": [{"sku": "AX", "warehouse": "SOUTH", "lot": "X", "lot_id": self.south.id, "quantity": 9}],
            "shortages": [{"sku": "AX", "missing": 5}],
        })
        self.assertEqual(self.inventory.rows.by_id(self.south.id).quantity, 9)

    def test_invalid_order_exchange_reports_shape(self):
        for data, message in [("{", "invalid order file"), ("{}", "invalid order file"),
                              ("[1]", "invalid order row"), ('[{"sku":"ax"}]', "invalid order row"),
                              ('[{"sku":"ax","quantity":1,"priority":2}]', "invalid order row")]:
            path = self.file("order.json", data)
            with self.subTest(data=data), self.assertRaisesRegex(StockroomError, message):
                load_requests(path)
        self.assertEqual(load_requests(self.file("empty.json", "[]")), [])
