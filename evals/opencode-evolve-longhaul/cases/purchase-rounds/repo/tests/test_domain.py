from tests.runtime import DeskTest


class MarketTests(DeskTest):
    def test_order_pipeline_excludes_orders_already_released(self):
        orders = self.get("purchase-orders/pipeline", {"scope": "north", "day": "2025-04-10"})
        self.assertEqual(
            orders,
            [
                {"supplier": "garden", "units": 3, "cents": 900, "orders": ["po-g"]},
                {
                    "supplier": "orchard",
                    "units": 21,
                    "cents": 9830,
                    "orders": ["po-a", "po-b", "po-c"],
                },
            ],
        )

    def test_shortage_due_is_reduced_by_existing_credit(self):
        rows = self.get(
            "delivery-lines/shortages",
            {"scope": "north", "first": "2025-04-01", "last": "2025-04-15"},
        )
        self.assertEqual(
            [(row["line"], row["units"], row["credit_due_cents"]) for row in rows],
            [("drop-a", 8, 3400), ("paper-drop-a", 3, 410)],
        )

    def test_paper_deposit_ignores_void_slips(self):
        rows = self.get("crate-moves/deposits", {"scope": "north", "day": "2025-04-15"})
        self.assertEqual(
            rows,
            [
                {"supplier": "garden", "crates": 4, "deposit_cents": 500},
                {"supplier": "orchard", "crates": 5, "deposit_cents": 750},
            ],
        )

    def test_agreed_prices_respect_inclusive_windows(self):
        params = {"scope": "north", "supplier": "orchard"}
        self.assertEqual(
            self.get("agreements/price", {**params, "day": "2025-04-30"}),
            [{"supplier": "orchard", "id": "spring-a", "cents": 405}],
        )
        self.assertEqual(self.get("agreements/price", {**params, "day": "2025-05-01"}), [])

    def test_cancellation_updates_legacy_report_and_is_audited(self):
        row = self.post("purchase-orders/cancel", {"id": "po-a"}, actor="clerk")
        self.assertEqual(row["status"], "cancelled")
        report = self.get("desk/report", {"scope": "north", "day": "2025-04-10"})
        self.assertEqual(report["draft_order_cents"], 6480)
        self.assertEqual(self.get("audit/list")[-1]["event"], "purchase-orders.cancelled")
