import csv
import io
import json
import subprocess
import sys
from copy import deepcopy

from tests.project import APP, EXAMPLE_TABLE, SCOPE_TABLE
from tests.runtime import ROOT, DeskTest


class RegisterTests(DeskTest):
    def state_rows(self, table):
        return list(self.state_data()["tables"][table].values())

    def test_fixture_round_trips_all_version_one_registers(self):
        export = self.get("bundle/export")["tables"]
        for table, rows in self.state_data()["tables"].items():
            with self.subTest(table=table):
                self.assertEqual(export[table], [rows[key] for key in sorted(rows)])
                for row in export[table]:
                    self.assertEqual(self.get(table + "/show", {"id": row["id"]}), row)

    def test_list_uses_scope_and_status_together(self):
        rows = self.state_rows(EXAMPLE_TABLE)
        pairs = sorted({(row["scope"], row["status"]) for row in rows})
        for scope, status in pairs:
            with self.subTest(scope=scope, status=status):
                expected = [row for row in rows if (row["scope"], row["status"]) == (scope, status)]
                self.assertEqual(
                    self.get(
                        EXAMPLE_TABLE + "/list",
                        {
                            "scope": scope,
                            "status": status,
                        },
                    ),
                    sorted(expected, key=lambda row: row["id"]),
                )

    def test_register_add_checks_existence_and_actor(self):
        table = SCOPE_TABLE
        args = {**self.state_rows(table)[0], "id": "east", "name": "Pavillon Est"}
        del args["version"]
        old = self.state.read_bytes()
        self.request("POST", table + "/add", args, actor="viewer", status=403)
        self.assert_unchanged(old)
        before = self.get("audit/list")
        result = self.post(table + "/add", args, actor="clerk")
        self.assertEqual(result, {**args, "version": 1})
        after = self.get("audit/list", {"after": before[-1]["seq"]})
        self.assertEqual(
            [(row["event"], row["actor"], row["record_id"]) for row in after],
            [(f"{table}.added", "clerk", "east")],
        )
        old = self.state.read_bytes()
        self.request("POST", table + "/add", args, actor="clerk", status=409)
        self.assert_unchanged(old)

    def test_register_amend_cannot_rekey_or_change_version(self):
        table = SCOPE_TABLE
        old = self.state.read_bytes()
        for changes in ({}, {"id": "other"}, {"version": 2}, {"unexpected": "field"}):
            with self.subTest(changes=changes):
                self.request(
                    "POST", table + "/amend", {"id": "north", "changes": changes}, status=400
                )
                self.assert_unchanged(old)

    def test_register_amend_cannot_move_fixed_references(self):
        old = self.state.read_bytes()
        row = self.state_rows(EXAMPLE_TABLE)[0]
        self.request(
            "POST",
            EXAMPLE_TABLE + "/amend",
            {
                "id": row["id"],
                "changes": {"scope": "south"},
            },
            status=400,
        )
        self.assert_unchanged(old)

    def test_register_amend_audits_only_an_actual_change(self):
        past = self.get("audit/list")
        table = SCOPE_TABLE
        result = self.post(table + "/amend", {"id": "north", "changes": {"name": "New Name"}})
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["name"], "New Name")
        event = self.get("audit/list")[-1]
        self.assertEqual(event["seq"], past[-1]["seq"] + 1)
        self.assertEqual(event["details"], {"fields": ["name"]})
        old = self.state.read_bytes()
        self.assertEqual(
            self.post(
                table + "/amend",
                {
                    "id": "north",
                    "changes": {"name": "New Name"},
                },
            ),
            result,
        )
        self.assert_unchanged(old)

    def test_unknown_scope_is_rejected_by_legacy_validator(self):
        row = deepcopy(self.state_rows(EXAMPLE_TABLE)[0])
        row = {k: v for k, v in row.items() if k != "version" and v is not None}
        row.update(id="unknown-desk", scope="missing")
        old = self.state.read_bytes()
        self.request("POST", EXAMPLE_TABLE + "/add", row, status=404)
        self.assert_unchanged(old)

    def test_overview_activity_counts_use_desk_and_day(self):
        day = "2025-04-08"
        report = self.get("desk/report", {"scope": "south", "day": day}, actor="viewer")
        rows = [
            row
            for row in self.state_rows(EXAMPLE_TABLE)
            if row["scope"] == "south" and row["day"] <= day
        ]
        counts = {
            status: sum(1 for row in rows if row["status"] == status)
            for status in {row["status"] for row in rows}
        }
        self.assertEqual(report["activity"][EXAMPLE_TABLE], counts)
        self.assertEqual(report["scope"], "south")
        self.assertEqual(report["day"], day)
        self.assertEqual(report["title"], self.get(SCOPE_TABLE + "/show", {"id": "south"})["name"])

    def test_export_uses_original_header_and_stable_record_order(self):
        answer = self.get(
            "desk/export", {"table": EXAMPLE_TABLE, "scope": "north", "last": "2025-04-04"}
        )
        rows = list(csv.DictReader(io.StringIO(answer["csv"])))
        expected = [
            row["id"]
            for row in self.state_rows(EXAMPLE_TABLE)
            if row["scope"] == "north" and row["day"] <= "2025-04-04"
        ]
        self.assertEqual(answer["count"], len(rows))
        self.assertEqual([row["id"] for row in rows], sorted(expected))
        self.assertTrue(all(row["version"] == "1" for row in rows))
        self.assertTrue(answer["csv"].startswith("id,version,scope,day,"))

    def test_exchange_cli_reads_legacy_rows_after_v2_write(self):
        self.post("store/upgrade", {"name": "bulletins"})
        read = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools/read_v1.py"), str(self.state), EXAMPLE_TABLE],
            check=True,
            text=True,
            capture_output=True,
            timeout=5,
        )
        result = json.loads(read.stdout)
        self.assertEqual(result, self.get(EXAMPLE_TABLE + "/list"))
        self.assertNotIn("bulletins", self.get("bundle/export")["tables"])

    def test_registered_fields_are_shared_by_surface(self):
        for verb in ("show", "list", "add", "amend"):
            command = APP.registry.by_name[SCOPE_TABLE, verb]
            self.assertEqual(command.path, f"/v1/{SCOPE_TABLE}/{verb}")
            self.assertEqual(APP.registry.by_route[command.method, command.path], command)
