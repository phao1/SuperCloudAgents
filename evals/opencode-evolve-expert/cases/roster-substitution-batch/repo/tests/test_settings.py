from unittest.mock import patch

from crewcall import settings, storage
from crewcall.desk import Desk
from crewcall.errors import DeskError

from tests.support import MON, TUE, DeskTest


class SettingsTests(DeskTest):
    def test_venues_and_unicode_display_names_survive_restart(self):
        added = self.desk.change(settings.venue, "west", "Salle d’été 🎶", "Europe/Berlin")
        self.assertEqual(Desk(self.path).state.venues["west"], added)
        self.assertIn("Salle d’été 🎶", self.path.read_text(encoding="utf-8"))
        self.assertEqual([row["code"] for row in self.json_cli("venue", "list")],
                         ["main", "studio", "west"])

    def test_venue_replacement_keeps_dependencies(self):
        self.rule()
        self.desk.change(settings.venue, "main", "Main foyer", "America/New_York")
        fresh = Desk(self.path)
        self.assertEqual(fresh.state.rules["open"].lane, "doors")
        self.assertEqual(fresh.state.lanes["doors"].venue, "main")
        self.assertEqual(fresh.state.venues["main"].zone, "America/New_York")

    def test_bad_zone_and_code_do_not_save(self):
        current = self.path.read_bytes()
        self.cli_error("unknown time zone", "venue", "put", "new", "Place", "Zulu/Unknown")
        self.cli_error("invalid venue code", "venue", "put", "z ü", "Place", "UTC")
        self.assertEqual(self.path.read_bytes(), current)

    def test_members_deduplicate_and_sort_roles(self):
        value = self.desk.change(settings.member, "Kai", "Kai", ["sound", "door", "door"])
        self.assertEqual(value.roles, ("door", "sound"))
        self.assertEqual(Desk(self.path).state.members["Kai"], value)

    def test_member_requires_a_role_and_a_nonblank_name(self):
        for name, roles, error in (("Tom", [], "a member needs a role"),
                                   ("  ", ["door"], "invalid display name"),
                                   ("Tom", ["foyer desk"], "invalid role code")):
            with self.subTest(error=error), self.assertRaisesRegex(DeskError, error):
                self.desk.change(settings.member, "tom", name, roles)
        self.assertNotIn("tom", self.desk.state.members)

    def test_lane_filter_does_not_list_unknown_venues(self):
        self.assertEqual([row["code"] for row in self.json_cli("lane", "list", "--venue", "studio")],
                         ["mix"])
        self.cli_error("unknown venue", "lane", "list", "--venue", "yet")
        self.cli_error("unknown venue", "lane", "put", "new", "yet", "door", "Entrance")

    def test_rule_duration_and_weekday_labels_are_strict(self):
        for options, error in (
            ({"minutes": True}, "invalid duration"), ({"minutes": 2881}, "invalid duration"),
            ({"weekdays": "mon,mon"}, "invalid weekdays"),
            ({"weekdays": "fri,Mon"}, "invalid weekdays"), ({"start": "9:00"}, "invalid clock time"),
            ({"first": "2024-2-09"}, "invalid date"), ({"lane": "off"}, "unknown lane"),
        ):
            with self.subTest(options=options), self.assertRaisesRegex(DeskError, error):
                self.rule(**options)
        self.assertEqual(Desk(self.path).state.rules, {})

    def test_rule_range_last_day_is_inclusive(self):
        self.rule(first=MON, through=TUE, weekdays="tue,mon")
        rows = self.json_cli("roster", "show", MON, "2024-05-13")
        self.assertEqual([row["day"] for row in rows], [MON, TUE])
        self.assertEqual(self.json_cli("rule", "list")[0]["weekdays"], [0, 1])

    def test_rule_end_before_start_does_not_replace_previous(self):
        first = self.rule()
        with self.assertRaisesRegex(DeskError, "rule ends before it starts"):
            self.rule(first=TUE, through=MON)
        self.assertEqual(Desk(self.path).state.rules["open"], first)

    def test_default_desk_has_no_file_until_it_has_a_mutation(self):
        target = self.path.parent / "fresh.json"
        desk = Desk(target)
        self.assertEqual(desk.state.rules, {})
        self.assertFalse(target.exists())

    def test_failed_write_leaves_in_memory_desk_unchanged(self):
        baseline = self.desk.state
        bytes_before = self.path.read_bytes()
        with (
            patch.object(storage, "write", side_effect=DeskError("write failed")),
            self.assertRaisesRegex(DeskError, "write failed"),
        ):
            self.rule()
        self.assertEqual(self.desk.state, baseline)
        self.assertEqual(self.path.read_bytes(), bytes_before)

    def test_invalid_state_fails_without_overwriting_original(self):
        bad = b'{"assignments":[{"member":"staff"}]}\n'
        self.path.write_bytes(bad)
        with self.assertRaisesRegex(DeskError, "invalid desk state"):
            Desk(self.path)
        self.assertEqual(self.path.read_bytes(), bad)
