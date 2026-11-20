from unittest import TestCase

from crewcall import assignments, calendar
from crewcall.desk import Desk

from tests.support import MON, TUE, DeskTest

AT = MON + "T02:03:04Z"


class PayrollTests(DeskTest):
    def test_report_merges_day_venue_and_member_in_order(self):
        self.rule(code="hall", weekdays="mon,tue", minutes=50)
        self.rule(code="mix", lane="mix", start="12:00", minutes=100)
        for shift, member in (("hall:" + MON, "lin"), ("mix:" + MON, "ada"),
                              ("hall:" + TUE, "ada"), ("hall:" + MON, "ada")):
            self.assign(shift, member)
        self.desk.change(assignments.record_break, "hall:" + MON, "ada", 10, 5)
        expected = [
            {"day": MON, "venue": "main", "member": "ada", "minutes": 45},
            {"day": MON, "venue": "main", "member": "lin", "minutes": 50},
            {"day": MON, "venue": "studio", "member": "ada", "minutes": 100},
            {"day": TUE, "venue": "main", "member": "ada", "minutes": 50},
        ]
        before = self.path.read_bytes()
        self.assertEqual(self.json_cli("payroll", "report", MON, TUE), expected)
        self.assertEqual(self.json_cli("payroll", "report", MON, TUE, "--venue", "studio"), expected[2:3])
        self.assertEqual(self.path.read_bytes(), before)

    def test_fully_unpaid_duty_is_omitted(self):
        self.rule(minutes=50)
        binding = self.assign()
        self.desk.change(assignments.record_break, binding.shift, binding.member, 0, 50)
        self.assertEqual(self.json_cli("payroll", "report", MON, MON), [])

    def test_duty_on_a_skipped_shift_is_omitted(self):
        self.rule()
        self.assign()
        self.json_cli("rule", "change", "open", MON, "--skip")
        self.assertEqual(self.json_cli("payroll", "report", MON, MON), [])
        self.assertEqual(len(Desk(self.path).state.assignments), 1)

    def test_report_rejects_an_unknown_venue_and_inverted_dates(self):
        self.cli_error("unknown venue", "payroll", "report", MON, MON, "--venue", "missing")
        self.cli_error("invalid reporting range", "payroll", "report", TUE, MON)


class CalendarTests(DeskTest):
    def test_empty_calendar_has_header_and_terminator(self):
        status, text, error = self.cli("calendar", "export", MON, MON, "--at", AT)
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(text.split("\r\n"),
                         ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Crewcall//Roster//EN",
                          "END:VCALENDAR", ""])

    def test_calendar_render_has_stable_ids_and_absolute_timestamps(self):
        self.rule(code="sound", lane="mix", weekdays="mon,tue", start="11:00", minutes=55)
        before = self.path.read_bytes()
        _, text, _ = self.cli("calendar", "export", MON, TUE, "--at", AT)
        self.assertIn("UID:sound:2024-05-06@crewcall\r\n", text)
        self.assertIn("DTSTART:20240506T090000Z\r\nDTEND:20240506T095500Z\r\n", text)
        self.assertTrue(text.index("UID:sound:2024-05-06") < text.index("UID:sound:2024-05-07"))
        self.assertEqual(text.count("DTSTAMP:20240506T020304Z"), 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_skipped_shifts_do_not_appear_in_legacy_export(self):
        self.rule(weekdays="mon,tue")
        self.json_cli("rule", "change", "open", MON, "--skip")
        _, text, _ = self.cli("calendar", "export", MON, TUE, "--at", AT)
        self.assertNotIn("UID:open:2024-05-06", text)
        self.assertIn("UID:open:2024-05-07", text)

    def test_invalid_export_time_is_a_command_error(self):
        self.cli_error("invalid UTC instant", "calendar", "export", MON, MON,
                       "--at", MON + "T02:03:04")


class WireTests(TestCase):
    def test_escape_normalizes_all_three_newline_spellings(self):
        self.assertEqual(calendar.escape("Front, foyer; A\\B\rC\nD\r\nE"),
                         r"Front\, foyer\; A\\B\nC\nD\nE")

    def test_folding_accounts_for_octets_on_continuations(self):
        original = "SUMMARY:" + "a" * 65 + "舞台" * 41
        physical = calendar.fold(original).split("\r\n")
        self.assertGreater(len(physical), 2)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in physical))
        self.assertTrue(all(line.startswith(" ") for line in physical[1:]))
        self.assertEqual(physical[0] + "".join(line[1:] for line in physical[1:]), original)
