import csv
import io
from copy import deepcopy

from tests.project import EXAMPLE_TABLE, SCOPE_TABLE
from tests.runtime import DeskTest


class ExchangeTests(DeskTest):
    def test_full_bundle_round_trip(self):
        data = self.get("bundle/export")
        self.assertEqual(data["format"], 1)
        self.assertGreater(len(data["tables"]), 5)
        self.state = self.root / "restored.json"
        result = self.post("bundle/import", {"bundle": data})
        self.assertEqual(result["count"], sum(len(rows) for rows in data["tables"].values()))
        self.assertEqual(self.get("bundle/export"), data)

    def test_import_is_idempotent(self):
        data = self.get("bundle/export")
        before = self.state.read_bytes()
        result = self.post("bundle/import", {"bundle": data})
        self.assertEqual(result["count"], 0)
        self.assert_unchanged(before)

    def test_import_cannot_overwrite_existing_legacy_record(self):
        data = self.get("bundle/export")
        altered = deepcopy(data)
        next(row for row in altered["tables"][SCOPE_TABLE] if row["id"] == "north")["name"] = "New"
        before = self.state.read_bytes()
        self.request("POST", "bundle/import", {"bundle": altered}, status=409)
        self.assert_unchanged(before)

    def test_bad_bundle_is_an_all_or_none_merge(self):
        data = self.get("bundle/export")
        data["tables"][EXAMPLE_TABLE][-1]["version"] = 2
        self.state = self.root / "new.json"
        self.request("POST", "bundle/import", {"bundle": data}, status=400)
        self.assertFalse(self.state.exists())

    def test_duplicate_bundle_id_is_rejected(self):
        data = self.get("bundle/export", {"table": SCOPE_TABLE})
        data["tables"][SCOPE_TABLE] += deepcopy(data["tables"][SCOPE_TABLE][:1])
        self.request("POST", "bundle/import", {"bundle": data}, status=400)

    def test_bundle_export_filter_cannot_read_extension_table(self):
        self.post("store/upgrade", {"name": "bulletins"})
        self.request("GET", "bundle/export", {"table": "bulletins"}, status=404)

    def test_csv_columns_and_day_filter(self):
        payload = self.get(
            "desk/export",
            {"table": "desk-memos", "scope": "north", "first": "2025-04-01", "last": "2025-04-01"},
        )
        self.assertEqual(payload["count"], 1)
        result = csv.DictReader(io.StringIO(payload["csv"]))
        self.assertEqual(
            list(result.fieldnames), ["id", "version", "scope", "day", "message", "author"]
        )
        self.assertEqual(next(result)["id"], "memo-north")
        self.assertEqual(list(result), [])

    def test_invalid_date_range_is_rejected(self):
        self.request(
            "GET",
            "desk/export",
            {"table": "desk-memos", "first": "2025-04-02", "last": "2025-04-01"},
            status=400,
        )

    def test_report_unknown_scope_is_an_error(self):
        self.request("GET", "desk/report", {"scope": "unknown", "day": "2025-04-01"}, status=404)
