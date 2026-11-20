from datetime import UTC, date, datetime, timedelta

from crewcall import clock, schedule, settings
from crewcall.desk import Desk
from crewcall.errors import DeskError

from tests.support import MON, TUE, WED, DeskTest


class ScheduleTests(DeskTest):
    def test_local_time_is_converted_at_the_venue_not_the_host(self):
        self.rule(lane="mix", start="13:15", minutes=75, weekdays="mon,tue")
        rows = schedule.between(self.desk.state, MON, TUE)
        self.assertEqual([row.start for row in rows], [
            datetime(2024, 5, 6, 11, 15, tzinfo=UTC), datetime(2024, 5, 7, 11, 15, tzinfo=UTC),
        ])
        self.assertTrue(all(row.end - row.start == timedelta(minutes=75) for row in rows))

    def test_rule_identity_is_stable_when_times_change(self):
        self.rule()
        before = schedule.get(self.desk.state, "open:" + MON)
        self.desk.change(settings.override, "open", MON, start="10:30")
        after = schedule.get(Desk(self.path).state, before.id)
        self.assertEqual((after.id, after.day, after.lane), (before.id, MON, "doors"))
        self.assertEqual(after.start - before.start, timedelta(minutes=30))
        self.assertEqual(after.end - after.start, timedelta(hours=2))

    def test_duration_override_keeps_the_original_start(self):
        self.rule()
        first = schedule.get(self.desk.state, "open:" + MON)
        self.desk.change(settings.override, "open", MON, minutes=39)
        self.assertEqual(schedule.get(self.desk.state, first.id).end, first.start + timedelta(minutes=39))
        self.assertEqual(schedule.get(self.desk.state, "open:2024-05-13").end.hour, 12)

    def test_skip_and_restore_keep_existing_assignment_record(self):
        self.rule()
        assigned = self.assign()
        self.cli_error("skip cannot include new hours", "rule", "change", "open", MON,
                       "--skip", "--minutes", "30")
        self.json_cli("rule", "change", "open", MON, "--skip")
        self.assertEqual(self.json_cli("roster", "show", MON, MON), [])
        self.assertEqual(Desk(self.path).state.assignments, (assigned,))
        self.json_cli("rule", "change", "open", MON)
        self.assertEqual(self.json_cli("roster", "crew", assigned.shift), ["ada"])

    def test_order_uses_instant_then_lane_then_identity(self):
        self.rule(code="b", start="11:00")
        self.rule(code="a", start="11:00")
        self.rule(code="c", lane="mix", start="13:00")
        self.rule(code="earliest", lane="mix", start="12:59")
        rows = self.json_cli("roster", "show", MON, MON)
        self.assertEqual([row["rule"] for row in rows], ["earliest", "a", "b", "c"])
        self.assertEqual([row["rule"] for row in self.json_cli("roster", "show", MON, MON,
                                                              "--venue", "studio")],
                         ["earliest", "c"])

    def test_empty_week_does_not_invent_rows_from_overrides(self):
        self.rule(weekdays="mon")
        self.desk.change(settings.override, "open", TUE, start="10:00")
        self.assertEqual(schedule.between(self.desk.state, TUE, WED), [])
        with self.assertRaisesRegex(DeskError, "unknown shift"):
            schedule.get(self.desk.state, "open:" + TUE)

    def test_schedule_validates_range_and_unknown_venue_on_empty_roster(self):
        for first, last in ((TUE, MON), ("2024-02-29", "2026-02-28")):
            with self.subTest(first=first, last=last), self.assertRaisesRegex(DeskError,
                                                                            "invalid reporting range"):
                schedule.between(self.desk.state, first, last)
        with self.assertRaisesRegex(DeskError, "unknown venue"):
            schedule.between(self.desk.state, MON, MON, venue="offsite")

    def test_clock_date_does_not_accept_loose_iso_forms(self):
        self.assertEqual(clock.day("2024-02-29"), date(2024, 2, 29))
        for value in ("2024-02-30", "20240229", "2024-2-09", None):
            with self.subTest(value=value), self.assertRaisesRegex(DeskError, "invalid date"):
                clock.day(value)

    def test_instant_accepts_both_utc_spellings(self):
        self.assertEqual(clock.instant(MON + "T10:00Z"), clock.instant(MON + "T10:00:00+00:00"))
        for value in (MON + "T10:00", MON + "T09:00-01:00", MON + " 10:00Z"):
            with self.subTest(value=value), self.assertRaisesRegex(DeskError, "invalid UTC instant"):
                clock.instant(value)
