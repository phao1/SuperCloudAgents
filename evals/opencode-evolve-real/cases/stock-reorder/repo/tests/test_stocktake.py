from stockroom.errors import Conflict, Missing, StockroomError
from stockroom.models import Observation
from stockroom.stocktake import Counts

from tests.support import FRIDAY, MONDAY, THURSDAY, TUESDAY, Case


class StocktakeTests(Case):
    def setUp(self):
        super().setUp()
        self.counts = Counts(self.connection)
        self.first = self.lot("X", 3)
        self.second = self.lot("Y", 5, day=TUESDAY)
        self.south = self.lot("X", 6, warehouse="SOUTH")

    def create(self):
        return self.counts.open("north", "  End-of-month audit ", THURSDAY)

    def test_open_snapshots_visible_lots_for_one_warehouse(self):
        zero = self.lot("Z", 1)
        self.inventory.adjust("AX", "NORTH", "Z", -1, TUESDAY)
        opened = self.create()
        self.assertEqual((opened.status, opened.label, opened.closed), ("open", "End-of-month audit", None))
        self.assertEqual(self.counts.review(opened.id),
                         [Observation(self.first.id, "AX", "X", 3, None),
                          Observation(self.second.id, "AX", "Y", 5, None)])
        self.assertNotIn(zero.id, [row.lot_id for row in self.counts.review(opened.id)])

    def test_observations_can_be_replaced_and_cleared(self):
        session = self.create()
        self.assertEqual(self.counts.count(session.id, self.first.id, 0).difference, -3)
        self.assertEqual(self.counts.count(session.id, self.first.id, 4).difference, 1)
        self.assertIsNone(self.counts.count(session.id, self.first.id, None).difference)
        self.assertEqual(self.counts.review(session.id, differences_only=True), [])
        self.assertEqual(self.inventory.lots("AX", "NORTH")[0].quantity, 3)

    def test_count_does_not_add_new_lots_to_an_open_sheet(self):
        session = self.create()
        added = self.lot("Z", 9)
        for excluded in (added.id, self.south.id):
            with self.subTest(lot_id=excluded), self.assertRaisesRegex(Missing, "lot outside stocktake"):
                self.counts.count(session.id, excluded, 8)

    def test_count_validates_numeric_limits(self):
        session = self.create()
        for quantity in (False, -1, 2**63):
            with self.subTest(quantity=quantity), self.assertRaisesRegex(StockroomError, "invalid quantity"):
                self.counts.count(session.id, self.first.id, quantity)
        self.assertTrue(all(row.observed is None for row in self.counts.review(session.id)))

    def test_close_converts_differences_to_audit_records(self):
        session = self.create()
        self.counts.count(session.id, self.first.id, 0)
        self.counts.count(session.id, self.second.id, 6)
        self.assertEqual([row.difference for row in self.counts.review(session.id, True)], [-3, 1])
        closed = self.counts.close(session.id, FRIDAY)
        self.assertEqual((closed.status, closed.closed), ("closed", FRIDAY))
        events = self.reporting.activity(kind="adjust")
        self.assertEqual([(row.lot, row.delta, row.memo) for row in events],
                         [("X", -3, f"stocktake {closed.id}"), ("Y", 1, f"stocktake {closed.id}")])
        self.assertEqual(self.reporting.audit(mismatches_only=True), [])

    def test_matching_counts_produce_no_movements(self):
        session = self.create()
        self.counts.count(session.id, self.first.id, 3)
        self.counts.count(session.id, self.second.id, 5)
        self.assertEqual(self.counts.close(session.id, FRIDAY).status, "closed")
        self.assertEqual(self.reporting.activity(kind="adjust"), [])

    def test_close_fails_when_any_count_is_missing(self):
        session = self.create()
        self.counts.count(session.id, self.first.id, 9)
        with self.assertRaisesRegex(Conflict, "count sheet is incomplete"):
            self.counts.close(session.id, FRIDAY)
        self.assertEqual(self.counts.list(status="open"), [session])
        self.assertEqual(self.inventory.rows.by_id(self.first.id).quantity, 3)

    def test_close_rolls_back_a_prior_adjustment_if_later_lot_changed(self):
        session = self.create()
        self.counts.count(session.id, self.first.id, 8)
        self.counts.count(session.id, self.second.id, 7)
        self.inventory.adjust("AX", "NORTH", "Y", 1, THURSDAY)
        with self.assertRaisesRegex(Conflict, "stock changed during count"):
            self.counts.close(session.id, FRIDAY)
        self.assertEqual(self.counts.list(status="open"), [session])
        self.assertEqual(self.inventory.rows.by_id(self.first.id).quantity, 3)
        self.assertEqual([row.delta for row in self.reporting.activity(kind="adjust")], [1])

    def test_abort_keeps_sheet_but_never_changes_quantities(self):
        session = self.create()
        self.counts.count(session.id, self.first.id, 9)
        aborted = self.counts.abort(session.id, FRIDAY)
        self.assertEqual((aborted.status, aborted.closed), ("aborted", FRIDAY))
        self.assertEqual(self.counts.review(session.id, True)[0].observed, 9)
        self.assertEqual(self.inventory.rows.by_id(self.first.id).quantity, 3)
        with self.assertRaisesRegex(Conflict, "stocktake is closed"):
            self.counts.count(session.id, self.first.id, 3)

    def test_stocktake_list_follows_id_and_filters(self):
        first = self.create()
        second = self.counts.open("SOUTH", "2nd", THURSDAY)
        third = self.create()
        aborted = self.counts.abort(third.id, FRIDAY)
        self.assertEqual(self.counts.list(), [first, second, aborted])
        self.assertEqual(self.counts.list("north", "open"), [first])
        self.assertEqual(self.counts.list("north", "aborted"), [aborted])
        self.assertEqual(self.counts.list("south", "aborted"), [])

    def test_stocktake_dates_and_warehouse_state(self):
        session = self.create()
        with self.assertRaisesRegex(StockroomError, "close before open"):
            self.counts.abort(session.id, MONDAY)
        self.catalog.warehouse_status("NORTH", False)
        with self.assertRaisesRegex(StockroomError, "inactive warehouse"):
            self.create()
        self.assertEqual(self.counts.abort(session.id, THURSDAY).status, "aborted")

    def test_count_can_close_an_empty_warehouse(self):
        self.catalog.add_warehouse("EMPTY", "Outside shed")
        session = self.counts.open("EMPTY", "Empty sheet", MONDAY)
        self.assertEqual(self.counts.review(session.id), [])
        self.assertEqual(self.counts.close(session.id, MONDAY).status, "closed")
