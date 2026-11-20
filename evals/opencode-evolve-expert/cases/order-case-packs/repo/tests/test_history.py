import csv
import io

from stockroom.errors import StockroomError
from stockroom.history import HistoricalLot, History

from tests.support import FRIDAY, MONDAY, THURSDAY, TUESDAY, Case


class HistoricalReportTests(Case):
    def setUp(self):
        super().setUp()
        self.history = History(self.connection)

    def test_latest_intake_after_cutoff_is_excluded(self):
        future = self.lot("B", 2, day=THURSDAY)
        present = self.lot("A", 3, day=TUESDAY)
        self.assertEqual(self.history.lots_at(MONDAY), [])
        self.assertEqual(self.history.lots_at(TUESDAY),
                         [HistoricalLot(present.id, "AX", "NORTH", "A", TUESDAY, 3, 1, TUESDAY)])
        self.assertEqual([row.lot_id for row in self.history.lots_at(THURSDAY)], [present.id, future.id])
        self.assertEqual(self.inventory.lots()[0].quantity, 3)

    def test_includes_negative_historical_balance_and_tracks_latest_day(self):
        original = self.lot("L", 7, day=THURSDAY)
        self.inventory.adjust("AX", "NORTH", "L", -3, TUESDAY)
        historical = self.history.lots_at(TUESDAY)
        self.assertEqual(historical, [HistoricalLot(original.id, "AX", "NORTH", "L", THURSDAY, -3, 1, TUESDAY)])
        self.assertEqual(self.inventory.lots()[0].quantity, 4)
        [latest] = self.history.lots_at(FRIDAY)
        self.assertEqual((latest.balance, latest.movements, latest.latest), (4, 2, THURSDAY))

    def test_fully_depleted_lot_can_be_included(self):
        original = self.lot("A", 3)
        self.inventory.ship("AX", "NORTH", 3, TUESDAY)
        self.assertEqual(self.history.lots_at(TUESDAY), [])
        self.assertEqual(self.history.lots_at(TUESDAY, include_zero=True),
                         [HistoricalLot(original.id, "AX", "NORTH", "A", MONDAY, 0, 2, TUESDAY)])
        self.assertEqual(self.history.lots_at(MONDAY)[0].balance, 3)

    def test_filters_and_transfer_history_use_both_distinct_lot_ids(self):
        initial = self.lot("A", 7)
        self.lot("B", 3, sku="SEED")
        [moved] = self.inventory.move("AX", "NORTH", "SOUTH", 5, THURSDAY)
        self.assertEqual(self.history.lots_at(TUESDAY, "ax", "south"), [])
        self.assertEqual(self.history.lots_at(THURSDAY, "ax"), [
            HistoricalLot(initial.id, "AX", "NORTH", "A", MONDAY, 2, 2, THURSDAY),
            HistoricalLot(moved.id, "AX", "SOUTH", "A", MONDAY, 5, 1, THURSDAY),
        ])
        self.assertEqual([row.sku for row in self.history.lots_at(THURSDAY, warehouse="north")], ["AX", "SEED"])

    def test_source_order_breaks_same_day_ties(self):
        first = self.lot("B", 2, day=TUESDAY)
        later = self.lot("A", 3, day=TUESDAY)
        early = self.lot("ZZ", 5, day=MONDAY)
        self.assertEqual([row.lot_id for row in self.history.lots_at(TUESDAY)], [early.id, first.id, later.id])
        self.assertEqual([row.lot_id for row in self.history.lots_at(MONDAY)], [early.id])

    def test_csv_and_json_formats_show_the_same_historical_data(self):
        lot = self.lot("A", 6)
        self.inventory.ship("AX", "NORTH", 1, TUESDAY)
        report = self.json_cli("report", "as-of", MONDAY, "--warehouse", "north")
        self.assertEqual(report, [{"lot_id": lot.id, "sku": "AX", "warehouse": "NORTH", "code": "A",
                                   "received": MONDAY, "balance": 6, "movements": 1, "latest": MONDAY}])
        code, csv_text, error = self.cli("report", "as-of", MONDAY, "--warehouse", "north", "--format", "csv")
        self.assertEqual((code, error), (0, ""))
        [record] = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(record, {key: str(value) for key, value in report[0].items()})

    def test_day_and_filter_validation_are_consistent_with_other_reports(self):
        with self.assertRaisesRegex(StockroomError, "invalid day"):
            self.history.lots_at("2024-02-30")
        with self.assertRaisesRegex(StockroomError, "invalid sku"):
            self.history.lots_at(MONDAY, sku="has spaces")
        with self.assertRaisesRegex(StockroomError, "invalid warehouse"):
            self.history.lots_at(MONDAY, warehouse="bad!")
        self.cli_error("invalid day", "report", "as-of", "2024-2-29")

    def test_cutoff_groups_backdated_moves_without_reading_current_quantity(self):
        initial = self.lot("A", 4, day=FRIDAY)
        self.inventory.adjust("AX", "NORTH", "A", 5, MONDAY)
        self.inventory.adjust("AX", "NORTH", "A", -2, TUESDAY)
        self.assertEqual(self.history.lots_at(MONDAY),
                         [HistoricalLot(initial.id, "AX", "NORTH", "A", FRIDAY, 5, 1, MONDAY)])
        self.assertEqual(self.history.lots_at(FRIDAY),
                         [HistoricalLot(initial.id, "AX", "NORTH", "A", FRIDAY, 7, 3, FRIDAY)])
