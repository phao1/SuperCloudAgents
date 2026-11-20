from tests.runtime import DeskTest


class BulletinTests(DeskTest):
    def post_bulletin(self, **changes):
        params = {
            "id": "new-bulletin",
            "scope": "north",
            "day": "2025-04-01",
            "expires": "2025-04-08",
            "message": "Lock the side door.",
        }
        params.update(changes)
        return self.post("bulletins/post", params, actor="clerk")

    def test_post_and_read_in_french(self):
        saved = self.post_bulletin(message="Sortie côté cour")
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["author"], "Desk clerk")
        self.assertEqual(
            self.request("GET", "bulletins/list", {"scope": "north"}, actor="viewer", locale="fr"),
            {"data": [saved], "message": "Avis du bureau"},
        )

    def test_post_uses_extension_schema(self):
        row = self.post_bulletin()
        self.assertEqual(
            self.state_data()["extensions"]["bulletins"], {"schema": 2, "records": {row["id"]: row}}
        )
        self.assertNotIn("bulletins", self.state_data()["tables"])

    def test_notices_filter_by_scope_and_status(self):
        first = self.post_bulletin()
        self.post_bulletin(id="side-room", scope="south")
        self.assertEqual(
            self.get("bulletins/list", {"scope": "north", "status": "posted"}), [first]
        )

    def test_expiration_is_inclusive(self):
        self.post_bulletin()
        for date, expected in (("2025-04-08", ["new-bulletin"]), ("2025-04-09", [])):
            report = self.get("desk/report", {"scope": "north", "day": date})
            self.assertEqual(report["visible_bulletins"], expected)

    def test_retirement_checks_role_and_visibility_date(self):
        self.post_bulletin()
        change = {"id": "new-bulletin", "day": "2025-04-05"}
        self.request("POST", "bulletins/retire", change, actor="clerk", status=403)
        retired = self.post("bulletins/retire", change)
        self.assertEqual(retired["status"], "retired")
        report = self.get("desk/report", {"scope": "north", "day": "2025-04-05"})
        self.assertEqual(report["visible_bulletins"], [])
        before = self.state.read_bytes()
        self.request("POST", "bulletins/retire", change, status=409)
        self.assert_unchanged(before)

    def test_bulletin_upgrade_preserves_legacy_view(self):
        before = self.get("bundle/export")
        upgraded = self.post("store/upgrade", {"name": "bulletins"})
        self.assertEqual(upgraded, {"name": "bulletins", "imported": 2})
        self.assertEqual(self.get("bundle/export"), before)
        rows = self.get("bulletins/list", {"scope": "north"})
        self.assertEqual([row["id"] for row in rows], ["memo-north"])
        self.assertEqual(rows[0]["expires"], rows[0]["day"])

    def test_replay_does_not_restore_retired_bulletin(self):
        self.post("store/upgrade", {"name": "bulletins"})
        self.post("bulletins/retire", {"id": "memo-north", "day": "2025-04-02"})
        before = self.state.read_bytes()
        result = self.post("store/upgrade", {"name": "bulletins"})
        self.assertEqual(result, {"name": "bulletins", "imported": 0})
        self.assert_unchanged(before)

    def test_upgrade_is_manager_only(self):
        before = self.state.read_bytes()
        self.request("POST", "store/upgrade", {"name": "bulletins"}, actor="clerk", status=403)
        self.assert_unchanged(before)

    def test_collision_rolls_back_history_and_audit(self):
        self.post_bulletin(id="memo-north")
        before = self.state.read_bytes()
        body = self.request("POST", "store/upgrade", {"name": "bulletins"}, status=409)
        self.assertEqual(body["error"]["code"], "conflict")
        self.assert_unchanged(before)
