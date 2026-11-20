from tests.runtime import DeskTest


class DepotTests(DeskTest):
    def test_capacity_subtracts_due_reservations_and_service(self):
        rows = self.get("tools/capacity", {"scope": "north", "day": "2025-04-10"})
        self.assertEqual(
            {row["tool"]: row["available"] for row in rows},
            {"driver": 6, "driver-mini": 4, "saw": 3},
        )
        after = self.get("tools/capacity", {"scope": "north", "day": "2025-04-12"})
        self.assertEqual(next(row for row in after if row["tool"] == "saw")["available"], 4)

    def test_pickup_sheet_follows_the_recorded_destination(self):
        result = self.get(
            "reservations/sheet",
            {
                "scope": "south",
                "first": "2025-04-01",
                "last": "2025-04-10",
            },
        )
        self.assertEqual([row["id"] for row in result], ["paper-handoff-a", "reserve-s"])
        self.assertEqual(result[0]["borrower"], "Alba Ruiz")
        self.assertEqual(result[0]["source"], "north")

    def test_cancelling_unblocks_capacity_and_keeps_original_version(self):
        result = self.post(
            "reservations/cancel", {"id": "reserve-a", "day": "2025-04-10"}, actor="clerk"
        )
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["status"], "cancelled")
        report = self.get("desk/report", {"scope": "north", "day": "2025-04-10"})
        self.assertEqual(report["due_reservations"], ["reserve-b"])
        self.assertEqual(report["available_units"], 16)
        old = self.state.read_bytes()
        self.request(
            "POST", "reservations/cancel", {"id": "reserve-a", "day": "2025-04-11"}, status=409
        )
        self.assert_unchanged(old)

    def test_return_charges_exclude_unreviewed_entries(self):
        result = self.get(
            "returns/charges", {"scope": "north", "first": "2025-04-01", "last": "2025-04-11"}
        )
        self.assertEqual(
            result, {"tool_cents": {"saw": 1750}, "total_cents": 1750, "pending": ["return-a"]}
        )

    def test_legacy_register_rejects_damage_above_returned_units(self):
        self.request(
            "POST", "returns/amend", {"id": "return-a", "changes": {"damaged": 5}}, status=400
        )
