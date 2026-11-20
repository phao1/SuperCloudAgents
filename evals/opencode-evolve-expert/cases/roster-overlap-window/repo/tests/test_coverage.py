from dataclasses import replace

from crewcall import coverage, settings
from crewcall.desk import Desk

from tests.support import MON, TUE, DeskTest


class CoverageTests(DeskTest):
    def test_unstaffed_hall_shows_a_gap(self):
        self.rule(minutes=30)
        [row] = coverage.board(self.desk.state, MON, MON)
        self.assertFalse(row.covered)
        self.assertEqual((row.role, row.crew, row.review), ("door", (), ()))
        [gap] = self.json_cli("board", "gaps", MON, MON)
        self.assertEqual(gap, {
            "day": MON, "venue": "main", "role": "door", "shifts": ["open:" + MON],
            "minutes": 30,
        })

    def test_coverage_and_assignments_are_read_only_independent_views(self):
        self.rule()
        first = self.assign(member="lin")
        second = self.assign(member="ada")
        before = self.path.read_bytes()
        [row] = self.json_cli("board", "coverage", MON, MON)
        self.assertEqual(row["crew"], ["ada", "lin"])
        self.assertTrue(row["covered"])
        self.assertEqual(self.json_cli("board", "gaps", MON, MON), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(Desk(self.path).state.assignments, (second, first))

    def test_changed_role_is_reported_without_deleting_assignment(self):
        self.rule()
        binding = self.assign()
        self.desk.change(settings.member, "ada", "Ada", ["sound"])
        [row] = self.json_cli("board", "coverage", MON, MON)
        self.assertFalse(row["covered"])
        self.assertEqual((row["crew"], row["review"]), ([], ["ada"]))
        self.assertEqual(Desk(self.path).state.assignments, (binding,))

    def test_good_crew_covers_shift_even_if_a_peer_needs_review(self):
        self.rule()
        self.assign(member="ada")
        self.assign(member="lin")
        self.desk.change(settings.member, "ada", "Ada", ["sound"])
        [row] = coverage.board(Desk(self.path).state, MON, MON)
        self.assertTrue(row.covered)
        self.assertEqual((row.review, row.crew), (("ada",), ("lin",)))

    def test_unresolved_legacy_member_keeps_coverage_row_visible(self):
        self.rule()
        self.assign()
        state = replace(self.desk.state, members={})
        [row] = coverage.board(state, MON, MON)
        self.assertEqual(row.review, ("ada",))
        self.assertEqual(row.crew, ())

    def test_gaps_group_in_civil_date_then_venue_order(self):
        self.rule(code="last", start="15:00", minutes=30)
        self.rule(code="first", start="09:00", minutes=90, weekdays="mon,tue")
        self.rule(code="mic", lane="mix", start="09:00", minutes=11, weekdays="mon,tue")
        self.assign("mic:" + MON, "pat")
        rows = coverage.gaps(Desk(self.path).state, MON, TUE)
        self.assertEqual(
            [(row.day, row.venue, row.shifts, row.minutes) for row in rows],
            [(MON, "main", ("first:" + MON, "last:" + MON), 120),
             (TUE, "main", ("first:" + TUE,), 90),
             (TUE, "studio", ("mic:" + TUE,), 11)],
        )

    def test_cancelled_shift_is_excluded_regardless_of_existing_crew(self):
        self.rule()
        self.assign()
        self.desk.change(settings.override, "open", MON, skip=True)
        self.assertEqual(self.json_cli("board", "coverage", MON, MON), [])
        self.assertEqual(self.json_cli("board", "gaps", MON, MON), [])
        self.assertEqual(len(Desk(self.path).state.assignments), 1)

    def test_venue_filter_applies_to_both_board_views(self):
        self.rule()
        self.rule(code="studio", lane="mix", minutes=9)
        rows = self.json_cli("board", "coverage", MON, MON, "--venue", "studio")
        self.assertEqual([row["shift"] for row in rows], ["studio:" + MON])
        gaps = self.json_cli("board", "gaps", MON, MON, "--venue", "main")
        self.assertEqual([row["shifts"] for row in gaps], [["open:" + MON]])

    def test_bad_report_bounds_and_venues_are_console_errors(self):
        self.cli_error("invalid reporting range", "board", "gaps", TUE, MON)
        self.cli_error("unknown venue", "board", "coverage", MON, MON, "--venue", "no")
        self.cli_error("invalid date", "board", "coverage", "2024-2-29", MON)

    def test_board_reports_nominal_day_and_sorts_in_absolute_time(self):
        self.rule(code="hall", start="00:15")
        self.rule(code="sound", lane="mix", start="01:00")
        rows = self.json_cli("board", "coverage", MON, MON)
        self.assertEqual([row["shift"] for row in rows], ["sound:" + MON, "hall:" + MON])
        self.assertEqual([row["day"] for row in rows], [MON, MON])
        self.assertEqual(rows[0]["start"], "2024-05-05T23:00:00Z")

    def test_gap_uses_elapsed_minutes_for_a_shift_crossing_midnight(self):
        self.rule(start="23:30", minutes=90)
        [gap] = self.json_cli("board", "gaps", MON, MON)
        self.assertEqual(gap["minutes"], 90)
        self.assertEqual(self.json_cli("board", "gaps", TUE, TUE), [])

    def test_editing_a_shift_recomputes_coverage_on_the_next_read(self):
        self.rule(start="11:00", minutes=60)
        before = self.json_cli("board", "coverage", MON, MON)
        self.desk.change(settings.override, "open", MON, start="09:30", minutes=45)
        after = self.json_cli("board", "coverage", MON, MON)
        self.assertEqual(before[0]["shift"], after[0]["shift"])
        self.assertEqual(before[0]["start"], MON + "T11:00:00Z")
        self.assertEqual(after[0]["start"], MON + "T09:30:00Z")
        self.assertEqual(after[0]["end"], MON + "T10:15:00Z")

    def test_title_and_label_updates_do_not_change_coverage_identity(self):
        self.rule()
        self.assign()
        before = coverage.board(self.desk.state, MON, MON)
        self.desk.change(settings.lane, "doors", "main", "door", "Porte – arrière")
        self.desk.change(settings.member, "ada", "Ada Lovelace", ["door"])
        self.assertEqual(coverage.board(Desk(self.path).state, MON, MON), before)
