from tests.runtime import DeskTest


class VenueTests(DeskTest):
    def test_calendar_includes_blocks_touching_window_edges(self):
        result = self.get(
            "bookings/calendar",
            {
                "scope": "north",
                "first": "2025-04-14",
                "last": "2025-04-15",
            },
        )
        self.assertEqual([row["id"] for row in result], ["block-a"])
        self.assertEqual(result[0]["end"], "2025-04-14")

    def test_workload_uses_corrected_minutes(self):
        result = self.get(
            "crew/workload",
            {
                "scope": "south",
                "first": "2025-04-05",
                "last": "2025-04-10",
            },
        )
        self.assertEqual(
            [(row["person"], row["minutes"]) for row in result], [("chi", 95), ("dev", 100)]
        )

    def test_performance_lists_are_sorted_and_exclude_cancellations(self):
        args = {"scope": "south", "day": "2025-04-08"}
        result = self.get("performances/audience", args)
        self.assertEqual(
            result,
            [
                {
                    "performance": "paper-notice-a",
                    "production": "blue",
                    "audience": ["members", "schools"],
                }
            ],
        )
        self.assertEqual(
            self.get("performances/audience", {"scope": "north", "day": "2025-04-05"}), []
        )

    def test_report_excludes_scheduler_drafts_from_paper_roster(self):
        report = self.get("desk/report", {"scope": "north", "day": "2025-04-10"})
        self.assertEqual(report["worked_minutes"], 180)
        self.assertEqual(report["called_crew"], [])
        result = self.get("calls/roster", {"scope": "north", "day": "2025-04-04"})
        self.assertEqual(
            [(row["person"], row["duty"]) for row in result],
            [("ana", "lighting"), ("bilal", "sound")],
        )

    def test_old_booking_amend_rejects_a_venue_with_too_few_seats(self):
        self.request(
            "POST", "bookings/amend", {"id": "big-block", "changes": {"scope": "north"}}, status=409
        )
