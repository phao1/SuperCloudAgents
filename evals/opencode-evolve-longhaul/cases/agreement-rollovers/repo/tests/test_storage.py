import json

from deskcore.storage import Store, Table

from tests.project import SCOPE_TABLE
from tests.runtime import DeskTest


class StorageTests(DeskTest):
    def test_linked_tables_coexist_at_two_versions(self):
        tables = {SCOPE_TABLE: Table(SCOPE_TABLE), "local-drafts": Table("local-drafts", 2)}
        store = Store(self.state, tables)
        stored = store.insert("local-drafts", {"id": "first", "note": "Changed"})
        self.assertEqual(stored, {"id": "first", "note": "Changed", "version": 2})
        self.assertTrue(store.changed)
        store.flush()
        self.assertFalse(store.changed)
        reopened = Store(self.state, tables)
        self.assertEqual(reopened.rows("local-drafts"), [stored])
        self.assertEqual(reopened.require(SCOPE_TABLE, "north")["version"], 1)

    def test_unknown_extensions_are_preserved(self):
        fixture = self.state_data()
        archive = {"schema": 2, "records": {"older": {"id": "older", "version": 2}}}
        fixture["extensions"] = {"older-app": archive}
        self.state.write_text(json.dumps(fixture))
        self.post("store/upgrade", {"name": "bulletins"})
        self.assertEqual(self.state_data()["extensions"]["older-app"], archive)

    def test_store_recognizes_invalid_extension_version(self):
        fixture = self.state_data()
        fixture["extensions"] = {"wrong": {"schema": 3, "records": {}}}
        self.state.write_text(json.dumps(fixture))
        self.request("GET", "store/describe", status=422)

    def test_describe_counts_registered_empty_tables(self):
        result = self.get("store/describe")
        self.assertIn({"name": "intake", "version": 2, "count": 0}, result["tables"])
        self.assertIn({"name": SCOPE_TABLE, "version": 1, "count": 2}, result["tables"])

    def test_new_workspace_can_be_saved_with_an_empty_register(self):
        self.state = self.root / "new-desk" / "state.json"
        result = self.post(
            "team/add", {"actor": "analyst", "name": "Data Analyst", "role": "reader"}
        )
        self.assertEqual(result["actor"], "analyst")
        self.assertEqual(self.state_data()["tables"], {})

    def test_store_put_of_same_content_is_noop(self):
        schema = {SCOPE_TABLE: Table(SCOPE_TABLE)}
        store = Store(self.state, schema)
        before = self.state.read_bytes()
        store.put(SCOPE_TABLE, store.require(SCOPE_TABLE, "north"))
        self.assertFalse(store.changed)
        store.flush()
        self.assert_unchanged(before)

    def test_preview_is_detached(self):
        schema = {SCOPE_TABLE: Table(SCOPE_TABLE)}
        store = Store(self.state, schema)
        preview = store.preview()
        preview.put(SCOPE_TABLE, {**store.require(SCOPE_TABLE, "north"), "name": "Changed"})
        self.assertTrue(preview.changed)
        self.assertFalse(store.changed)
        self.assertNotEqual(
            store.require(SCOPE_TABLE, "north"), preview.require(SCOPE_TABLE, "north")
        )

    def test_audit_appends_respecting_existing_sequence(self):
        before = self.get("audit/list")
        self.post("team/add", {"actor": "test", "name": "Test Reader", "role": "reader"})
        events = self.get("audit/list")
        self.assertEqual(events[:-1], before)
        self.assertEqual(events[-1]["seq"], len(before) + 1)
        self.assertEqual(events[-1]["version"], 1)
