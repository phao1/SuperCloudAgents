from tests.support import DeskTest


class CommandTests(DeskTest):
    def publish(self, stream, *, kind="landed", stamp="15"):
        return self.json_cli("publish", stream, kind, "跟踪", '{"packages":2}', "--at", stamp)

    def test_publish_and_journal_share_one_record_shape(self):
        first = self.publish("dock.arrivals", stamp="50")
        last = self.publish("dock.in", kind="ready", stamp="2")
        self.assertEqual(self.json_cli("journal", "--after", "1"), [last])
        self.assertEqual(self.json_cli("journal", "--limit", "1"), [first])
        self.assertEqual(self.json_cli("journal"), [first, last])
        self.assertEqual(last["key"], "跟踪")

    def test_routes_survive_independent_commands(self):
        saved = self.json_cli("register", "early", "dock.*", "--exclude", "dock.private")
        self.assertEqual(saved["include"], ["dock.*"])
        self.assertEqual(self.json_cli("routes", "dock.in"), ["early"])
        self.assertEqual(self.json_cli("routes", "dock.private"), [])
        self.assertEqual(self.json_cli("unregister", "early"), saved)
        self.assertEqual(self.json_cli("routes", "dock.in"), [])

    def test_summary_uses_lexical_stream_and_kind_order(self):
        self.publish("w.y", kind="ready")
        self.publish("a.b", kind="unload")
        self.publish("a.b", kind="landed")
        self.publish("a.b", kind="unload")
        streams = self.json_cli("summary")
        self.assertEqual([row["stream"] for row in streams], ["a.b", "w.y"])
        self.assertEqual(streams[0], {"stream": "a.b", "first": 2, "last": 4,
                                      "count": 3, "kinds": {"landed": 1, "unload": 2}})
        self.assertEqual(list(streams[0]["kinds"]), ["landed", "unload"])
        self.assertEqual(self.json_cli("summary", "--stream", "w.y"), [streams[1]])

    def test_watch_cli_projects_route_batch(self):
        self.json_cli("register", "operator", "yard")
        self.publish("yard.a")
        last = self.publish("yard")
        self.assertEqual(self.json_cli("watch", "operator", "--after", "1"),
                         {"rows": [last], "after": 2})

    def test_parse_and_validation_fail_without_output(self):
        self.cli_error("invalid stream", "routes", "dock.*")
        self.cli_error("unknown route", "watch", "unknown")
        self.cli_error("invalid payload", "publish", "dock", "ready", "x", "[]", "--at", "4")
        self.assertEqual(self.json_cli("journal"), [])

    def test_archive_round_trip_reads_and_writes_existing_journal(self):
        self.publish("dock")
        self.publish("yard")
        archive = str(self.root / "events.hr")
        self.assertEqual(self.json_cli("archive", "export", archive, "--after", "1"), {"exported": 1})
        self.assertEqual(self.json_cli("archive", "import", archive), {"sequences": [3]})
        self.assertEqual([row["stream"] for row in self.json_cli("journal")], ["dock", "yard", "yard"])

    def test_prune_reports_count_and_retained_summary(self):
        self.publish("dock")
        self.publish("yard")
        self.assertEqual(self.json_cli("prune", "1"), {"removed": 1})
        self.assertEqual([row["stream"] for row in self.json_cli("summary")], ["yard"])
