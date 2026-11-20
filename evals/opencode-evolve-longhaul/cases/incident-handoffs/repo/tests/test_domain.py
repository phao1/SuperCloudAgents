from tests.runtime import DeskTest


class FieldDeskTests(DeskTest):
    def test_itinerary_keeps_other_station_stops_and_sorts(self):
        rows = self.get("visits/itinerary", {"crew": "brook", "day": "2025-04-10"})
        self.assertEqual([row["id"] for row in rows], ["visit-b", "visit-c", "visit-a"])
        self.assertEqual([row["scope"] for row in rows], ["south", "north", "north"])

    def test_meter_threshold_uses_absolute_deviation_and_latest_day(self):
        args = {"scope": "north", "day": "2025-04-04"}
        initial = self.get("meters/tolerances", args)
        self.assertEqual(
            [(row["meter"], row["outside_limit"]) for row in initial],
            [("depth", True), ("temp", False)],
        )
        later = self.get("meters/tolerances", {**args, "day": "2025-04-10", "meter": "depth"})
        self.assertEqual(
            later,
            [
                {
                    "day": "2025-04-10",
                    "meter": "depth",
                    "outside_limit": False,
                    "reading": 3,
                    "tolerance": 4,
                }
            ],
        )

    def test_sample_register_filters_type_and_sorts_by_day(self):
        rows = self.get(
            "samples/register",
            {"scope": "north", "water": "stream", "first": "2025-04-01", "last": "2025-04-10"},
        )
        self.assertEqual(rows["counts"], {"cleared": 1, "discarded": 1, "logged": 2})
        self.assertEqual(
            [row["id"] for row in rows["rows"]],
            ["paper-sample-a", "paper-sample-b", "sample-a", "sample-b"],
        )

    def test_closing_incident_removes_it_from_urgent_register(self):
        params = {"scope": "north", "day": "2025-04-10"}
        before = self.get("incidents/urgent", params)
        self.assertIn("incident-a", [row["id"] for row in before])
        self.post("incidents/close", {"id": "incident-a", "reason": "Crew returned"}, actor="clerk")
        report = self.get("desk/report", params)
        self.assertEqual(report["open_urgent_incidents"], ["paper-incident-a"])

    def test_meter_scope_validation_checks_the_legacy_reference(self):
        self.request(
            "GET",
            "meters/tolerances",
            {"scope": "north", "day": "2025-04-02", "meter": "cond"},
            status=409,
        )
