from stockroom.errors import StockroomError
from stockroom.models import Aging, Balance, Trend
from stockroom.trend import bucket

from tests.support import FRIDAY, MONDAY, THURSDAY, TUESDAY, Case


class ReportingTests(Case):
    def populated(self):
        self.lot("RECENT", 9, day=FRIDAY)
        self.lot("B", 3)
        self.lot("A", 2, day=TUESDAY)
        self.lot("A", 1, sku="SEED")
        self.lot("B", 4, warehouse="SOUTH")

    def test_balances_aggregate_positive_lots_by_item_and_warehouse(self):
        self.populated()
        self.inventory.adjust("AX", "NORTH", "B", -3, FRIDAY)
        self.assertEqual(self.reporting.balances(),
                         [Balance("AX", "NORTH", 11, 2), Balance("SEED", "NORTH", 1, 1),
                          Balance("AX", "SOUTH", 4, 1)])
        self.assertEqual(self.reporting.balances("ax", "south"), [Balance("AX", "SOUTH", 4, 1)])
        self.assertEqual(self.reporting.balances("seed", "south"), [])

    def test_balance_omits_a_fully_depleted_group(self):
        self.lot(quantity=2)
        self.inventory.ship("AX", "NORTH", 2, TUESDAY)
        self.assertEqual(self.reporting.balances(), [])
        self.assertEqual(len(self.inventory.lots(include_empty=True)), 1)

    def test_activity_orders_by_day_then_id(self):
        self.populated()
        rows = self.reporting.activity(warehouse="north", sku="ax")
        self.assertEqual([(row.day, row.lot) for row in rows],
                         [(MONDAY, "B"), (TUESDAY, "A"), (FRIDAY, "RECENT")])
        self.assertNotEqual([row.id for row in rows], sorted(row.id for row in rows))

    def test_activity_combines_kind_dates_and_limit(self):
        self.populated()
        self.inventory.adjust("AX", "NORTH", "B", 2, MONDAY, "first")
        self.inventory.adjust("AX", "NORTH", "B", 1, MONDAY, "second")
        self.inventory.adjust("AX", "NORTH", "B", -1, TUESDAY, "third")
        rows = self.reporting.activity("AX", "NORTH", MONDAY, MONDAY, "adjust", 1)
        self.assertEqual([(row.delta, row.memo) for row in rows], [(2, "first")])
        self.assertEqual(len(self.reporting.activity(since=TUESDAY, through=TUESDAY)), 2)

    def test_activity_validates_range_kind_and_limit(self):
        with self.assertRaisesRegex(StockroomError, "invalid day range"):
            self.reporting.activity(since=FRIDAY, through=MONDAY)
        with self.assertRaisesRegex(StockroomError, "invalid movement kind"):
            self.reporting.activity(kind="adjustment")
        with self.assertRaisesRegex(StockroomError, "invalid quantity"):
            self.reporting.activity(limit=0)

    def test_aging_is_based_on_remaining_lots_within_the_day(self):
        self.populated()
        self.inventory.adjust("AX", "NORTH", "B", -3, TUESDAY)
        self.assertEqual(self.reporting.aging(THURSDAY),
                         [Aging("AX", "NORTH", 2, 2, 2), Aging("SEED", "NORTH", 1, 3, 3),
                          Aging("AX", "SOUTH", 4, 3, 3)])
        self.assertEqual(self.reporting.aging(FRIDAY, "AX", "NORTH"), [Aging("AX", "NORTH", 11, 0, 3)])
        self.assertEqual(self.reporting.aging("2024-02-25"), [])

    def test_audit_shows_zero_lots_and_exact_adjustment_deltas(self):
        self.lot("A", 3)
        self.inventory.ship("AX", "NORTH", 3, TUESDAY)
        [row] = self.reporting.audit("ax", "north")
        self.assertEqual((row.code, row.quantity, row.recorded, row.difference), ("A", 0, 0, 0))
        self.assertEqual(self.reporting.audit(mismatches_only=True), [])

    def test_audit_reports_unlogged_stock_changes_only_as_mismatches(self):
        self.lot("GOOD", 3)
        flagged = self.lot("FLAG", 5)
        with self.connection:
            self.connection.execute("UPDATE lot SET quantity = 6 WHERE id = ?", (flagged.id,))
        [row] = self.reporting.audit(mismatches_only=True)
        self.assertEqual((row.lot_id, row.quantity, row.recorded, row.difference), (flagged.id, 6, 5, 1))
        self.assertEqual(self.reporting.audit(warehouse="SOUTH", mismatches_only=True), [])

    def test_periods_use_gregorian_days(self):
        self.assertEqual(bucket(FRIDAY, "day"), FRIDAY)
        self.assertEqual(bucket(FRIDAY, "week"), MONDAY)
        self.assertEqual(bucket(THURSDAY, "month"), "2024-02-01")
        with self.assertRaisesRegex(StockroomError, "invalid trend period"):
            self.reporting.trend("year")

    def test_trend_includes_each_transfer_side_and_net(self):
        self.lot("A", 7)
        self.inventory.adjust("AX", "NORTH", "A", 1, TUESDAY)
        self.inventory.move("AX", "NORTH", "SOUTH", 3, THURSDAY)
        self.inventory.ship("AX", "NORTH", 1, FRIDAY)
        self.assertEqual(self.reporting.trend("week"),
                         [Trend(MONDAY, "AX", "NORTH", 7, 1, 1, -3, 4, 4),
                          Trend(MONDAY, "AX", "SOUTH", 0, 0, 0, 3, 3, 1)])

    def test_trend_dates_filter_before_bucket_aggregation(self):
        self.lot("A", 5)
        self.inventory.ship("AX", "NORTH", 5, FRIDAY)
        self.assertEqual(self.reporting.trend("week", "ax", "north", since=FRIDAY),
                         [Trend(MONDAY, "AX", "NORTH", 0, 5, 0, 0, -5, 1)])
        self.assertEqual(self.reporting.trend("week", "seed"), [])
