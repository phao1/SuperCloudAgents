import csv
import io
import json

from deskcore.fields import bind, id_field, int_field
from deskcore.storage import Store, Table

from tests.project import APP, SCOPE_TABLE
from tests.runtime import DeskTest


class RuntimeTests(DeskTest):
    def test_help_and_routes_share_registration(self):
        for command in APP.registry:
            self.assertEqual(APP.registry.by_name[command.key], command)
            self.assertEqual(APP.registry.by_route[command.method, command.path], command)

    def test_all_commands_have_a_role_and_distinct_translations(self):
        for command in APP.registry:
            self.assertIn(command.permission, APP.policy)
            self.assertIn(command.message, APP.messages["en"])
            self.assertIn(command.message, APP.messages["fr"])
            self.assertNotEqual(
                APP.messages["en"][command.message], APP.messages["fr"][command.message]
            )

    def test_reading_an_absent_desk_does_not_create_it(self):
        self.state.unlink()
        result = self.get("store/describe")
        self.assertEqual(result["format"], 1)
        self.assertFalse(self.state.exists())

    def test_routes_do_not_accept_a_different_method(self):
        reply = self.request("POST", "store/describe", {}, status=404)
        self.assertEqual(reply["error"]["code"], "not_found")

    def test_unknown_argument_is_rejected_before_the_handler(self):
        before = self.state.read_bytes()
        body = self.request("POST", "team/suspend", {"actor": "viewer", "active": True}, status=400)
        self.assertIn("active", body["error"]["detail"])
        self.assert_unchanged(before)

    def test_inactive_operator_cannot_read_or_write(self):
        self.post("team/suspend", {"actor": "clerk"})
        for method, route, params in (
            ("GET", "store/describe", {}),
            ("POST", "bulletins/retire", {"id": "missing", "day": "2025-04-02"}),
        ):
            body = self.request(method, route, params, actor="clerk", status=403)
            self.assertEqual(body["error"]["code"], "forbidden")

    def test_only_manager_lists_team_members(self):
        self.request("GET", "team/list", actor="clerk", status=403)
        self.assertEqual(
            [row["actor"] for row in self.get("team/list")], ["clerk", "lead", "ridge", "viewer"]
        )

    def test_member_registration_preserves_unicode(self):
        value = "Léa Noël"
        self.post("team/add", {"actor": "lea.noel", "name": value, "role": "operator"})
        self.assertEqual(self.get("team/whoami", actor="lea.noel")["name"], value)
        self.assertIn(value.encode(), self.state.read_bytes())

    def test_repeated_suspension_does_not_repeat_audit(self):
        self.post("team/suspend", {"actor": "clerk"})
        before = self.state.read_bytes()
        self.post("team/suspend", {"actor": "clerk"})
        self.assert_unchanged(before)

    def test_self_suspension_is_rejected(self):
        before = self.state.read_bytes()
        self.request("POST", "team/suspend", {"actor": "lead"}, status=409)
        self.assert_unchanged(before)

    def test_audit_paging_and_filter_are_exclusive(self):
        self.post("team/add", {"actor": "fran", "name": "Fran", "role": "reader"})
        self.post("team/add", {"actor": "jo", "name": "Jo", "role": "operator"})
        self.post("team/suspend", {"actor": "fran"})
        all_events = self.get("audit/list", actor="clerk")
        last = all_events[-3:]
        self.assertEqual(
            [row["event"] for row in last], ["team.added", "team.added", "team.suspended"]
        )
        self.assertEqual(
            self.get("audit/list", {"after": last[0]["seq"], "limit": 1}, actor="clerk"), [last[1]]
        )
        self.assertEqual(
            self.get("audit/list", {"event": "team.suspended"}, actor="clerk"), [last[2]]
        )

    def test_audit_csv_uses_same_event_order(self):
        self.post("team/add", {"actor": "pat", "name": "Pat", "role": "operator"})
        result = self.get("audit/export", {"event": "team.added"})
        rows = list(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(result["count"], len(rows))
        self.assertEqual(rows[-1]["record_id"], "pat")
        self.assertEqual(json.loads(rows[-1]["details"]), {"role": "operator"})

    def test_missing_legacy_record_is_not_found(self):
        body = self.request(
            "GET", SCOPE_TABLE + "/show", {"id": "missing"}, status=404, locale="fr"
        )
        self.assertEqual(body["error"]["message"], "Enregistrement ou commande introuvable")

    def test_cli_matches_api(self):
        args = {"id": "north"}
        body = self.request("GET", SCOPE_TABLE + "/show", args, locale="fr", actor="viewer")
        self.assertEqual(
            self.cli(SCOPE_TABLE, "show", "--id", "north", actor="viewer", locale="fr"), body
        )

    def test_cli_error_payload_matches_api(self):
        reply = self.request("GET", "team/list", actor="viewer", locale="fr", status=403)
        self.assertEqual(self.cli("team", "list", actor="viewer", locale="fr", returncode=1), reply)

    def test_number_rejects_booleans_and_strings(self):
        fields = (id_field(), int_field("count", "count", 1))
        for candidate in (True, False, "2", 0):
            from deskcore.errors import DeskError

            with self.subTest(candidate=candidate), self.assertRaises(DeskError):
                bind(fields, {"id": "abc", "count": candidate})

    def test_bad_store_keeps_bytes(self):
        self.state.write_text('{"format":2}', encoding="utf-8")
        self.request("GET", "store/describe", status=422)
        self.assertEqual(self.state.read_bytes(), b'{"format":2}')

    def test_persisted_version_is_checked_for_each_record(self):
        data = self.state_data()
        data["tables"][SCOPE_TABLE]["north"]["version"] = True
        self.state.write_text(json.dumps(data), encoding="utf-8")
        self.request("GET", "store/describe", status=422)

    def test_store_returns_detached_rows(self):
        store = Store(self.state, {SCOPE_TABLE: Table(SCOPE_TABLE)})
        row = store.require(SCOPE_TABLE, "north")
        row["name"] = "temporary"
        self.assertNotEqual(store.require(SCOPE_TABLE, "north")["name"], "temporary")
        self.assertFalse(store.changed)
