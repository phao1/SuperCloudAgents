from tests.project import SCOPE_TABLE
from tests.runtime import DeskTest


class FollowupTests(DeskTest):
    def params(self, **changes):
        return {
            "id": "check",
            "register": "desk-memos",
            "source": "memo-north",
            "owner": "clerk",
            "day": "2025-04-08",
            "due": "2025-04-11",
            "priority": "normal",
            "note": "Call Léa",
            **changes,
        }

    def queue(self, **filters):
        return self.get("followups/list", {"scope": "north", **filters}, actor="viewer")

    def results(self, day):
        return self.get("desk/report", {"scope": "north", "day": day}, actor="viewer")["followups"]

    def test_source_scope_is_inferred_for_regular_register(self):
        row = self.post("followups/open", self.params(), actor="clerk")
        self.assertEqual(row["scope"], "north")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["version"], 2)
        self.assertEqual(row["note"], "Call Léa")
        self.assertEqual(self.queue(), [row])

    def test_location_is_its_own_scope(self):
        row = self.post("followups/open", self.params(register=SCOPE_TABLE, source="south"))
        self.assertEqual(row["scope"], "south")
        self.assertEqual(self.queue(), [])

    def test_followup_does_not_edit_source(self):
        before = self.get("bundle/export")
        self.post("followups/open", self.params())
        self.post("followups/close", {"id": "check", "day": "2025-04-11", "outcome": "Confirmed"})
        self.assertEqual(self.get("bundle/export"), before)

    def test_another_operator_cannot_change_assignee_or_deadline(self):
        self.post("followups/open", self.params())
        old = self.state.read_bytes()
        for route, params in (
            ("assign", {"owner": "ridge"}),
            ("reschedule", {"due": "2025-04-15", "reason": "Desk closed"}),
            ("close", {"day": "2025-04-11", "outcome": "Confirmed"}),
        ):
            self.request(
                "POST", "followups/" + route, {"id": "check", **params}, actor="ridge", status=403
            )
            self.assert_unchanged(old)

    def test_reassignment_changes_who_can_act(self):
        self.post("followups/open", self.params())
        self.post("followups/assign", {"id": "check", "owner": "ridge"})
        change = {"id": "check", "due": "2025-04-15", "reason": "Venue closed"}
        self.request("POST", "followups/reschedule", change, actor="clerk", status=403)
        rescheduled = self.post("followups/reschedule", change, actor="ridge")
        self.assertEqual(rescheduled["due"], "2025-04-15")
        self.assertEqual(self.queue(owner="clerk"), [])
        event = self.get("audit/list", {"event": "followups.rescheduled"})[-1]
        self.assertEqual(
            event["details"],
            {
                "previous": "2025-04-11",
                "due": "2025-04-15",
                "reason": "Venue closed",
            },
        )

    def test_report_is_bound_by_open_and_closed_dates(self):
        self.post("followups/open", self.params())
        self.post(
            "followups/close",
            {"id": "check", "day": "2025-04-14", "outcome": "Confirmed"},
            actor="clerk",
        )
        for date, total, overdue, today in (
            ("2025-04-07", 0, [], []),
            ("2025-04-08", 1, [], []),
            ("2025-04-11", 1, [], ["check"]),
            ("2025-04-13", 1, ["check"], []),
            ("2025-04-14", 0, [], []),
        ):
            with self.subTest(date=date):
                self.assertEqual(
                    self.results(date),
                    {
                        "open": total,
                        "overdue": overdue,
                        "due_today": today,
                    },
                )

    def test_second_open_task_for_source_is_rejected(self):
        self.post("followups/open", self.params())
        old = self.state.read_bytes()
        self.request("POST", "followups/open", self.params(id="again"), status=409)
        self.assert_unchanged(old)
        self.post("followups/close", {"id": "check", "day": "2025-04-11", "outcome": "Done"})
        self.post("followups/open", self.params(id="again", day="2025-04-12", due="2025-04-16"))
        self.assertEqual([row["id"] for row in self.queue(status="open")], ["again"])

    def test_suspended_operator_and_reader_cannot_be_assignees(self):
        self.post("team/suspend", {"actor": "ridge"})
        old = self.state.read_bytes()
        for owner in ("ridge", "viewer", "missing"):
            self.request("POST", "followups/open", self.params(owner=owner), status=400)
        self.assert_unchanged(old)

    def test_no_task_can_be_due_before_opening(self):
        old = self.state.read_bytes()
        self.request("POST", "followups/open", self.params(due="2025-04-07"), status=400)
        self.assert_unchanged(old)
        self.post("followups/open", self.params())
        old = self.state.read_bytes()
        self.request(
            "POST",
            "followups/close",
            {
                "id": "check",
                "day": "2025-04-07",
                "outcome": "Cannot predate",
            },
            status=400,
        )
        self.assert_unchanged(old)

    def test_options_come_from_registered_command_fields(self):
        row = self.cli(
            "followups",
            "open",
            "--id",
            "check",
            "--register",
            "desk-memos",
            "--source",
            "memo-north",
            "--owner",
            "clerk",
            "--day",
            "2025-04-08",
            "--due",
            "2025-04-11",
            "--priority",
            "normal",
            "--note",
            "Call Léa",
            actor="clerk",
            locale="fr",
        )
        self.assertEqual(row["message"], "Suivi ouvert")
        reply = self.request("GET", "followups/show", {"id": "check"}, actor="viewer")
        self.assertEqual(row["data"], reply["data"])

    def test_explicit_sorting_by_due_and_priority(self):
        for task in (
            self.params(id="beta", source="memo-north", due="2025-04-10"),
            self.params(
                id="gamma",
                register=SCOPE_TABLE,
                source="north",
                due="2025-04-10",
                priority="urgent",
            ),
        ):
            self.post("followups/open", task)
        self.assertEqual([row["id"] for row in self.queue()], ["gamma", "beta"])
        self.assertEqual([row["id"] for row in self.queue(priority="normal")], ["beta"])

    def test_missing_legacy_source_gives_not_found(self):
        self.request("POST", "followups/open", self.params(source="missing"), status=404)
