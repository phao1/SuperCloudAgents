from collections.abc import Callable

from deskcore.application import Application, Command, Registry
from deskcore.errors import fail
from deskcore.storage import Table

from tests.runtime import DeskTest


class DispatchTests(DeskTest):
    def command(self, handler: Callable, method="POST", permission="demo.write"):
        return Command(
            "demo",
            "test",
            method,
            "/v1/demo/test",
            permission,
            "demo.test",
            "Example command",
            (),
            handler,
        )

    def application(self, command):
        return Application(
            [command],
            [Table("demo", 2)],
            {"demo.write": "operator"},
            {"en": {"demo.test": "Demo complete"}, "fr": {"demo.test": "Test terminé"}},
            ["demo.written"],
            {},
        )

    def test_duplicate_routes_and_commands_are_rejected(self):
        def handle(ctx):
            return {}

        command = self.command(handle)
        same_name = Command(
            "demo",
            "test",
            "GET",
            "/v1/demo/get",
            "demo.write",
            "demo.test",
            "Duplicate",
            (),
            handle,
        )
        same_route = Command(
            "demo",
            "write",
            "POST",
            "/v1/demo/test",
            "demo.write",
            "demo.test",
            "Duplicate",
            (),
            handle,
        )
        for duplicate in (same_name, same_route):
            with self.subTest(duplicate=duplicate.key), self.assertRaises(ValueError):
                Registry([command, duplicate])

    def test_read_handler_is_not_allowed_to_save(self):
        def write(ctx):
            return ctx.store.insert("demo", {"id": "test-row"})

        app = self.application(self.command(write, method="GET"))
        before = self.state.read_bytes()
        reply = app.request(self.state, "lead", "GET", "/v1/demo/test")
        self.assertEqual(reply.status, 400)
        self.assertIn("read-only", reply.body["error"]["detail"])
        self.assert_unchanged(before)

    def test_error_unwinds_staged_writes_and_audit(self):
        def write_then_reject(ctx):
            ctx.store.insert("demo", {"id": "test-row"})
            ctx.audit("demo.written", "test-row", location="north")
            fail("conflict", "late validation failed")

        app = self.application(self.command(write_then_reject))
        before = self.state.read_bytes()
        reply = app.request(self.state, "lead", "POST", "/v1/demo/test")
        self.assertEqual(reply.status, 409)
        self.assert_unchanged(before)

    def test_unknown_event_aborts_the_command(self):
        def write_bad_event(ctx):
            ctx.store.insert("demo", {"id": "test-row"})
            ctx.audit("demo.unknown", "test-row")

        app = self.application(self.command(write_bad_event))
        before = self.state.read_bytes()
        reply = app.request(self.state, "lead", "POST", "/v1/demo/test")
        self.assertEqual(reply.status, 400)
        self.assertIn("unknown audit event", reply.body["error"]["detail"])
        self.assert_unchanged(before)

    def test_missing_policy_denies_even_manager_before_mutating(self):
        def handler(ctx):
            self.fail("handler ran before authorization")

        app = self.application(self.command(handler, permission="demo.unbound"))
        before = self.state.read_bytes()
        reply = app.request(self.state, "lead", "POST", "/v1/demo/test")
        self.assertEqual(reply.status, 403)
        self.assert_unchanged(before)

    def test_new_store_does_not_serialize_preview_extension(self):
        def handler(ctx):
            ctx.store.preview().insert("demo", {"id": "temporary"})
            return {"count": len(ctx.store.rows("demo"))}

        path = self.root / "new.json"
        app = self.application(self.command(handler))
        reply = app.request(path, "clerk", "POST", "/v1/demo/test", locale="fr")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"data": {"count": 0}, "message": "Test terminé"})
        self.assertFalse(path.exists())

    def test_request_validates_bad_dates_before_writes(self):
        before = self.state.read_bytes()
        result = self.request(
            "POST",
            "bulletins/post",
            {
                "id": "invalid",
                "scope": "north",
                "day": "2025-02-29",
                "expires": "2025-04-01",
                "message": "Wrong calendar day",
            },
            status=400,
        )
        self.assertIn("YYYY-MM-DD", result["error"]["detail"])
        self.assert_unchanged(before)

    def test_cli_json_binding_and_versioned_update(self):
        from tests.project import SCOPE_TABLE

        result = self.cli(
            SCOPE_TABLE,
            "amend",
            "--id",
            "north",
            "--changes",
            '{"name": "Château du Nord"}',
            actor="clerk",
        )
        self.assertEqual(result["data"]["name"], "Château du Nord")
        self.assertEqual(result["data"]["version"], 1)
        self.assertEqual(self.get(SCOPE_TABLE + "/show", {"id": "north"}), result["data"])

    def test_localized_input_error_preserves_desk(self):
        before = self.state.read_bytes()
        result = self.cli("audit", "list", "--limit", "large", locale="fr", returncode=2)
        self.assertEqual(result["error"]["message"], "Requête invalide")
        self.assert_unchanged(before)

    def test_registry_rejects_repeated_field_descriptors(self):
        from deskcore.fields import id_field

        def handle(ctx, **kwargs):
            return kwargs

        command = Command(
            "demo",
            "test",
            "POST",
            "/v1/demo/test",
            "demo.write",
            "demo.test",
            "Duplicate field",
            (id_field(), id_field()),
            handle,
        )
        with self.assertRaises(ValueError):
            Registry([command])
