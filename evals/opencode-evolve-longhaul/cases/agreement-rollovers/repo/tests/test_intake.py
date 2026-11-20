from copy import deepcopy

from tests.project import SCOPE_TABLE
from tests.runtime import DeskTest


class IntakeTests(DeskTest):
    def bundle(self):
        return self.get("bundle/export", {"table": SCOPE_TABLE})

    def new_location(self, record_id="east", title="East Desk"):
        data = self.bundle()
        records = data["tables"][SCOPE_TABLE]
        new = {**records[0], "id": record_id, "name": title}
        records.append(new)
        return data

    def prepare(self, name="delivery-a", bundle=None, source="District office", **options):
        return self.post(
            "intake/prepare",
            {
                "id": name,
                "source": source,
                "bundle": bundle if bundle is not None else self.new_location(),
            },
            **options,
        )

    def test_prepare_saves_review_without_importing_locations(self):
        old = self.bundle()
        result = self.prepare(actor="clerk")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["prepared_by"], "clerk")
        self.assertEqual(result["tables"], {SCOPE_TABLE: {"add": 1, "unchanged": 2}})
        self.assertEqual(self.bundle(), old)
        self.assertEqual(
            self.state_data()["extensions"]["intake"]["records"]["delivery-a"]["version"], 2
        )

    def test_apply_writes_new_rows_and_can_be_replayed(self):
        self.prepare(actor="clerk")
        result = self.post("intake/apply", {"id": "delivery-a"})
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["reviewed_by"], "lead")
        self.assertEqual(result["inserted"], 1)
        location = self.get(SCOPE_TABLE + "/show", {"id": "east"})
        self.assertEqual(location["name"], "East Desk")
        self.assertEqual(location["version"], 1)
        before = self.state.read_bytes()
        self.assertEqual(self.post("intake/apply", {"id": "delivery-a"}), result)
        self.assert_unchanged(before)

    def test_prepare_error_leaves_no_review_or_legacy_changes(self):
        bundle = self.new_location()
        conflicting = bundle["tables"][SCOPE_TABLE][0]
        conflicting["name"] = "Wrong desk"
        before = self.state.read_bytes()
        body = self.request(
            "POST",
            "intake/prepare",
            {
                "id": "delivery-a",
                "source": "District office",
                "bundle": bundle,
            },
            status=409,
        )
        self.assertEqual(body["error"]["code"], "conflict")
        self.assert_unchanged(before)

    def test_cancelled_review_cannot_import(self):
        self.prepare()
        payload = self.post("intake/cancel", {"id": "delivery-a", "reason": "Use revised bundle"})
        self.assertEqual(payload["reason"], "Use revised bundle")
        before = self.state.read_bytes()
        self.request("POST", "intake/apply", {"id": "delivery-a"}, status=409)
        self.assert_unchanged(before)

    def test_legacy_edit_can_stale_the_review(self):
        self.prepare()
        self.post(SCOPE_TABLE + "/amend", {"id": "north", "changes": {"name": "New District"}})
        before = self.state.read_bytes()
        reply = self.request("POST", "intake/apply", {"id": "delivery-a"}, status=409)
        self.assertIn("changed since", reply["error"]["detail"])
        self.assert_unchanged(before)

    def test_disjoint_legacy_edit_keeps_the_review_current(self):
        bundle = self.new_location()
        bundle["tables"][SCOPE_TABLE] = [bundle["tables"][SCOPE_TABLE][-1]]
        self.prepare(bundle=bundle)
        self.post(SCOPE_TABLE + "/amend", {"id": "north", "changes": {"name": "New District"}})
        result = self.post("intake/apply", {"id": "delivery-a"})
        self.assertEqual(result["inserted"], 1)

    def test_operator_can_preview_but_only_manager_can_confirm(self):
        self.prepare(actor="clerk")
        before = self.state.read_bytes()
        self.request("POST", "intake/apply", {"id": "delivery-a"}, actor="clerk", status=403)
        self.request(
            "POST",
            "intake/cancel",
            {
                "id": "delivery-a",
                "reason": "No",
            },
            actor="clerk",
            status=403,
        )
        self.assert_unchanged(before)

    def test_filter_and_sort_review_queue(self):
        plain = self.bundle()
        self.prepare("review-c", plain, "Desk C")
        self.prepare("review-a", plain, "Desk B")
        self.prepare("review-b", plain, "Desk C")
        self.post("intake/cancel", {"id": "review-c", "reason": "Duplicate"})
        rows = self.get("intake/list", {"source": "Desk C", "status": "ready"}, actor="viewer")
        self.assertEqual([row["id"] for row in rows], ["review-b"])
        self.assertEqual(
            [row["id"] for row in self.get("intake/list")], ["review-a", "review-b", "review-c"]
        )

    def test_apply_does_not_expose_bundle_or_digest(self):
        record = self.prepare()
        self.assertNotIn("bundle", record)
        self.assertNotIn("fingerprint", record)
        self.assertEqual(self.get("intake/show", {"id": "delivery-a"}), record)

    def test_blank_bundle_is_rejected(self):
        before = self.state.read_bytes()
        self.request(
            "POST",
            "intake/prepare",
            {
                "id": "delivery-a",
                "source": "District office",
                "bundle": {"format": 1, "tables": {}},
            },
            status=400,
        )
        self.assert_unchanged(before)

    def test_audit_contains_actor_and_review_details(self):
        self.prepare(actor="clerk")
        self.post("intake/apply", {"id": "delivery-a"})
        entries = self.get("audit/list")[-3:]
        self.assertEqual(
            [row["event"] for row in entries],
            ["intake.prepared", "bundle.imported", "intake.applied"],
        )
        self.assertEqual([row["actor"] for row in entries], ["clerk", "lead", "lead"])
        self.assertEqual(entries[-1]["details"], {"source": "District office", "inserted": 1})

    def test_review_summary_survives_an_unrelated_upgrade(self):
        old = deepcopy(self.prepare())
        self.post("store/upgrade", {"name": "bulletins"})
        self.assertEqual(self.get("intake/show", {"id": "delivery-a"}), old)
