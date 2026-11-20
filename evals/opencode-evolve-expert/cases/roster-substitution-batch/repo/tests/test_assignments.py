from crewcall import assignments, review
from crewcall.desk import Desk
from crewcall.errors import DeskError

from tests.support import MON, DeskTest


class RosterTests(DeskTest):
    def setUp(self):
        super().setUp()
        self.rule()
        self.shift = "open:" + MON

    def test_several_crew_can_share_one_shift(self):
        self.assign(self.shift, "lin")
        self.assign(self.shift, "ada")
        fresh = Desk(self.path)
        self.assertEqual(assignments.crew(fresh.state, self.shift), ["ada", "lin"])
        self.assertEqual(self.json_cli("roster", "crew", self.shift), ["ada", "lin"])

    def test_unknown_member_missing_role_and_unknown_shift_are_distinct(self):
        before = self.path.read_bytes()
        for shift, member, error in (
            (self.shift, "none", "unknown member"),
            (self.shift, "pat", "member lacks shift role"),
            ("absent:" + MON, "ada", "unknown shift"),
        ):
            self.cli_error(error, "roster", "assign", shift, member)
        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_assignment_and_double_removal_are_rejected(self):
        binding = self.assign()
        self.cli_error("already assigned", "roster", "assign", binding.shift, binding.member)
        self.assertEqual(self.json_cli("roster", "unassign", binding.shift, binding.member),
                         {"shift": binding.shift, "member": binding.member})
        self.cli_error("unknown assignment", "roster", "unassign", binding.shift, binding.member)

    def test_unassign_removes_one_members_breaks(self):
        self.assign(self.shift, "ada")
        self.assign(self.shift, "lin")
        for member in ("ada", "lin"):
            self.desk.change(assignments.record_break, self.shift, member, 10, 15)
        self.desk.change(assignments.remove, self.shift, "ada")
        fresh = Desk(self.path)
        self.assertEqual([row.member for row in fresh.state.breaks], ["lin"])
        self.assertEqual(assignments.crew(fresh.state, self.shift), ["lin"])

    def test_break_range_is_relative_to_elapsed_duration(self):
        self.assign()
        actual = self.desk.change(assignments.record_break, self.shift, "ada", 119, 1)
        self.assertEqual((actual.offset, actual.minutes), (119, 1))
        for offset, minutes, error in ((121, 1, "offset"), (119, 2, "duration"),
                                       (False, 1, "offset"), (0, True, "duration")):
            with self.subTest(offset=offset, minutes=minutes), self.assertRaisesRegex(
                DeskError, f"invalid break {error}",
            ):
                self.desk.change(assignments.record_break, self.shift, "ada", offset, minutes)
        self.assertEqual(Desk(self.path).state.breaks, (actual,))

    def test_break_requires_an_assignment(self):
        self.cli_error("unknown assignment", "roster", "break", self.shift, "ada", "0", "15")
        self.assertEqual(Desk(self.path).state.breaks, ())

    def test_conflicts_order_distinct_shifts_and_ignore_other_members(self):
        self.rule(code="two", start="11:30")
        self.rule(code="other", lane="mix", start="12:30")
        self.assign()
        self.assign("two:" + MON, "ada")
        self.assign("other:" + MON, "pat")
        rows = self.json_cli("roster", "conflicts", "ada", MON + "T00:00Z", MON + "T23:59Z")
        self.assertEqual(rows, [{
            "member": "ada", "first": self.shift, "second": "two:" + MON,
            "start": MON + "T11:30:00Z", "end": MON + "T12:00:00Z",
        }])

    def test_touching_shifts_and_invalid_review_windows(self):
        self.rule(code="afternoon", start="12:00")
        self.assign()
        self.assign("afternoon:" + MON, "ada")
        start = MON + "T00:00Z"
        self.assertEqual(review.conflicts(self.desk.state, "ada", start, MON + "T23:59Z"), [])
        with self.assertRaisesRegex(DeskError, "invalid review window"):
            review.conflicts(self.desk.state, "ada", start, start)
        with self.assertRaisesRegex(DeskError, "unknown member"):
            review.conflicts(self.desk.state, "unlisted", start, MON + "T23:59Z")
