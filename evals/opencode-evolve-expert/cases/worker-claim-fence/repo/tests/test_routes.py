from unittest.mock import patch

from harborrelay.directory import Directory
from harborrelay.dispatch import replenish
from harborrelay.errors import RelayError
from harborrelay.leases import Worklist
from harborrelay.routing import RoutingTable, preview
from harborrelay.watch import Watch

from tests.support import DeskTest, notice


class RouteTests(DeskTest):
    def test_registration_deduplicates_patterns_and_preserves_slot(self):
        initial = self.directory.register("Morning", ("dock.*", "dock.*", "North"))
        self.directory.register("Night", ("dock.in",))
        self.assertEqual(initial.include, ("dock.*", "North"))
        self.directory.register("Morning", ("dock.*",), ("dock.private", "dock.private"))
        rows = Directory(self.home).list()
        self.assertEqual([row.name for row in rows], ["Morning", "Night"])
        self.assertEqual(rows[0].exclude, ("dock.private",))

    def test_dropped_route_removes_selection_and_can_be_readded_last(self):
        self.directory.register("First", ("dock.*",))
        second = self.directory.register("Second", ("dock.*",))
        self.assertEqual(self.directory.drop("First").name, "First")
        self.assertEqual(self.directory.list(), (second,))
        self.directory.register("First", ("dock.*",))
        self.assertEqual(preview(self.directory, "dock.in"), ["Second", "First"])

    def test_includes_and_excludes_are_anded_per_route(self):
        self.directory.register("wide", ("dock.*",), ("dock.private",))
        self.directory.register("private", ("dock.private",))
        self.assertEqual(preview(self.directory, "dock.private"), ["private"])
        self.assertEqual(preview(self.directory, "dock.arrivals"), ["wide"])
        self.assertEqual(preview(self.directory, "dock.arrivals.early"), [])
        self.assertEqual(preview(self.directory, "Dock.arrivals"), [])

    def test_explicit_match_any_segment_keeps_fixed_depth(self):
        route = self.directory.register("routes", ("*.arrivals.*", "exact"))
        table = RoutingTable([route])
        self.assertEqual(table.targets("north.arrivals.sunrise"), ["routes"])
        self.assertEqual(table.targets("north.arrivals"), [])
        self.assertEqual(table.targets("exact"), ["routes"])
        self.assertEqual(table.targets("exact.suffix"), [])

    def test_validation_applies_to_every_pattern(self):
        for pattern in ("", "a..b", "a*", ".bad", "a.한글"):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(RelayError, "invalid pattern"):
                self.directory.register("one", ("valid", pattern))
        with self.assertRaisesRegex(RelayError, "route needs an inclusion"):
            self.directory.register("one", ())
        self.assertEqual(self.directory.list(), ())

    def test_save_failure_keeps_current_routes(self):
        first = self.directory.register("one", ("North",))
        with (
            patch("harborrelay.directory.replace_bytes", side_effect=RelayError("write failed")),
            self.assertRaisesRegex(RelayError, "write failed"),
        ):
            self.directory.register("one", ("South",))
        self.assertEqual(self.directory.list(), (first,))
        self.assertEqual(Directory(self.home).list(), (first,))

    def test_corrupt_directory_does_not_become_empty_routes(self):
        self.directory.register("one", ("active",))
        data = self.directory.path.read_bytes()
        self.directory.path.write_bytes(b"[]" + data)
        with self.assertRaisesRegex(RelayError, "invalid route directory"):
            Directory(self.home)
        self.assertEqual(self.directory.list()[0].include, ("active",))

    def test_watch_selects_matches_in_sequence_not_timestamp_order(self):
        self.directory.register("desk", ("dock.*",), ("dock.private",))
        self.journal.append_many([notice("dock.private"), notice(stamp=9), notice(stamp=2)])
        found = Watch(self.journal, self.directory).read("desk")
        self.assertEqual([row.seq for row in found.rows], [2, 3])
        self.assertEqual(found.after, 3)

    def test_watch_applies_size_before_filtering(self):
        self.directory.register("desk", ("dock.*",))
        self.journal.append_many([notice("other.in"), notice(), notice()])
        watch = Watch(self.journal, self.directory)
        self.assertEqual([r.seq for r in watch.read("desk", limit=2).rows], [2])
        self.assertEqual([r.seq for r in watch.read("desk", after=2, limit=1).rows], [3])

    def test_watch_validates_route_before_reading(self):
        with self.assertRaisesRegex(RelayError, "unknown route"):
            Watch(self.journal, self.directory).read("missing")
        with self.assertRaisesRegex(RelayError, "invalid route"):
            self.directory.get("bad route")

    def test_dispatch_only_enqueues_route_members_and_is_repeatable(self):
        self.directory.register("crew", ("dock.*",), ("dock.private",))
        self.journal.append_many([notice(), notice("dock.private"), notice()])
        queue = Worklist(lambda: 10)
        self.assertEqual(replenish(queue, self.journal, self.directory, "crew", after=1), 1)
        self.assertEqual(queue.pending(), [3])
        self.assertEqual(replenish(queue, self.journal, self.directory, "crew"), 1)
        self.assertEqual(queue.pending(), [1, 3])
        self.assertEqual(replenish(queue, self.journal, self.directory, "crew"), 0)
