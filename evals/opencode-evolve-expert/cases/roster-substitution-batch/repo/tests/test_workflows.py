import json
import subprocess
import sys

from crewcall import assignments, settings
from crewcall.desk import Desk

from tests.support import MON, TUE, DeskTest


class WorkflowTests(DeskTest):
    def test_python_module_entry_point_reads_same_desk_file(self):
        self.rule(weekdays="mon,tue", minutes=30)
        self.assign()
        output = subprocess.run(
            [sys.executable, "-B", "-m", "crewcall", "--desk", str(self.path),
             "payroll", "report", MON, TUE],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(output.stderr, "")
        self.assertEqual(json.loads(output.stdout), [
            {"day": MON, "venue": "main", "member": "ada", "minutes": 30},
        ])

    def test_desk_keeps_old_and_new_rules_after_separate_cli_sessions(self):
        self.json_cli("rule", "put", "early", "doors", "mon", "08:00", "180", MON)
        self.json_cli("rule", "put", "audio", "mix", "tue", "14:30", "90", TUE)
        self.json_cli("roster", "assign", "early:" + MON, "lin")
        self.json_cli("roster", "assign", "audio:" + TUE, "pat")
        self.json_cli("roster", "break", "audio:" + TUE, "pat", "20", "15")
        rows = self.json_cli("payroll", "report", MON, TUE)
        self.assertEqual([(row["member"], row["minutes"]) for row in rows], [("lin", 180), ("pat", 75)])
        self.assertEqual(set(Desk(self.path).state.rules), {"early", "audio"})

    def test_promoting_a_rule_to_a_new_lane_reuses_its_day_id(self):
        self.rule()
        assigned = self.assign()
        self.rule(lane="mix", start="10:20")
        [row] = self.json_cli("roster", "show", MON, MON)
        self.assertEqual((row["id"], row["lane"], row["start"]),
                         (assigned.shift, "mix", MON + "T08:20:00Z"))
        self.assertEqual(self.json_cli("roster", "crew", assigned.shift), ["ada"])

    def test_report_ignores_assignments_for_retired_recurrences(self):
        self.rule()
        assigned = self.assign()
        self.rule(weekdays="tue")
        self.assertEqual(self.json_cli("payroll", "report", MON, TUE), [])
        self.assertEqual(Desk(self.path).state.assignments, (assigned,))
        self.cli_error("unknown shift", "roster", "crew", assigned.shift)

    def test_unassign_can_archive_a_staff_entry_for_a_cancelled_shift(self):
        self.rule()
        binding = self.assign()
        self.desk.change(assignments.record_break, binding.shift, "ada", 0, 15)
        self.desk.change(settings.override, "open", MON, skip=True)
        self.assertEqual(self.json_cli("roster", "unassign", binding.shift, "ada")["member"], "ada")
        self.json_cli("rule", "change", "open", MON)
        current = Desk(self.path).state
        self.assertEqual(current.breaks, ())
        self.assertEqual(current.assignments, ())
        self.assertEqual(self.json_cli("roster", "crew", binding.shift), [])

    def test_same_clock_label_at_two_sites_is_ordered_by_instant(self):
        self.rule(code="A", lane="doors", start="08:30")
        self.rule(code="B", lane="mix", start="08:30")
        rows = self.json_cli("roster", "show", MON, MON)
        self.assertEqual([(row["rule"], row["start"]) for row in rows], [
            ("B", MON + "T06:30:00Z"), ("A", MON + "T08:30:00Z"),
        ])

    def test_day_validation_is_independent_of_whether_crew_exist(self):
        path = self.path.parent / "fresh.json"
        output = subprocess.run(
            [sys.executable, "-B", "-m", "crewcall", "--desk", str(path),
             "roster", "show", "2024-02-30", "2024-03-01"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual((output.returncode, output.stdout, output.stderr), (2, "", "error: invalid date\n"))
        self.assertFalse(path.exists())
