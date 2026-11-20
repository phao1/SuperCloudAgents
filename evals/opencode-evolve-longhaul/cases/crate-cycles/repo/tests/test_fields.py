import unittest
from datetime import date

from deskcore.errors import DeskError
from deskcore.fields import (
    actor_key,
    bind,
    choice,
    date_field,
    day,
    decode_integer,
    decode_json,
    id_field,
    ids_field,
    int_field,
    json_field,
    key,
    keys,
    number,
    object_fields,
    optional_date,
    optional_int,
    text,
)
from deskcore.query import between, csv_text, duration, overlaps, selected, totals, window


class FieldTests(unittest.TestCase):
    def invalid(self, function, *args):
        with self.assertRaises(DeskError) as context:
            function(*args)
        self.assertEqual(context.exception.code, "bad_request")

    def test_dates_are_extended_calendar_dates(self):
        self.assertEqual(day("2024-02-29"), "2024-02-29")
        for candidate in ("2025-02-29", "20250402", "2025-W04-1", date(2025, 4, 2), " 2025-04-02"):
            with self.subTest(candidate=candidate):
                self.invalid(day, candidate)

    def test_ids_are_exact(self):
        self.assertEqual(key("x-2"), "x-2")
        for candidate in ("X-2", " two", "1east", "a/b", "a\n", "a" * 49, None):
            with self.subTest(candidate=candidate):
                self.invalid(key, candidate)

    def test_member_login_allows_dots_and_underscores(self):
        self.assertEqual(actor_key("lea.noel_2"), "lea.noel_2")
        self.invalid(actor_key, "lea@example.org")

    def test_normalized_text_preserves_inside_whitespace(self):
        self.assertEqual(text("  Ligne\n  deux "), "Ligne\n  deux")
        self.invalid(text, " " * 5)
        self.invalid(text, "x" * 501)

    def test_choices_must_be_exact(self):
        check = choice("draft", "ready")
        self.assertEqual(check("draft"), "draft")
        self.invalid(check, "Draft")

    def test_numeric_bounds_and_integer_conversion(self):
        check = number(-2, 3)
        self.assertEqual([check(-2), check(3)], [-2, 3])
        for candidate in (-3, 4, 2.5):
            self.invalid(check, candidate)
        self.assertEqual(decode_integer("-10"), -10)
        self.invalid(decode_integer, "2.5")

    def test_id_arrays_have_unique_entries_and_keep_user_order(self):
        self.assertEqual(keys(["beta", "alpha"]), ["beta", "alpha"])
        for candidate in ([], ["alpha", "alpha"], ("alpha",), ["alpha", 2]):
            self.invalid(keys, candidate)

    def test_json_values_and_fields(self):
        mapping = decode_json('{"ids":["alpha","beta"],"total":2}')
        self.assertEqual(mapping, {"ids": ["alpha", "beta"], "total": 2})
        self.invalid(decode_json, '{"count":NaN}')
        self.invalid(decode_json, "{[]}")
        for item in ([("a", 2)], {4: "name"}):
            self.invalid(object_fields, item)

    def test_dynamic_fields_set_validated_choices_and_defaults(self):
        fields = (
            id_field(),
            date_field(),
            ids_field("items", "list of IDs"),
            optional_date("start", "dates only"),
            optional_int("credit", "credit in cents"),
        )
        result = bind(fields, {"id": "run", "day": "2025-04-01", "items": ["one", "two"]})
        self.assertEqual(
            result,
            {
                "id": "run",
                "day": "2025-04-01",
                "items": ["one", "two"],
                "start": None,
                "credit": None,
            },
        )
        self.invalid(bind, fields, {"id": "run", "day": "2025-04-01"})
        self.invalid(
            bind, fields, {"id": "run", "day": "2025-04-01", "items": ["one"], "credit": None}
        )

    def test_console_argument_spellings(self):
        self.assertEqual(int_field("total_cents", "total").option, "--total-cents")
        self.assertEqual(json_field("copy", "JSON").cli('{"version":1}'), {"version": 1})


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"id": "a", "scope": "north", "day": "2025-03-31", "units": 2},
            {"id": "b", "scope": "south", "day": "2025-04-01", "units": 4},
            {"id": "c", "scope": "north", "day": "2025-04-02", "units": 3},
        ]

    def test_date_filters_are_inclusive_and_compose(self):
        filtered = between(selected(self.rows, scope="north"), "day", "2025-03-31", "2025-04-02")
        self.assertEqual([row["id"] for row in filtered], ["a", "c"])
        self.assertEqual(between(self.rows, "day", None, "2025-03-31"), [self.rows[0]])
        self.assertEqual(between(self.rows, "day", "2025-04-02", None), [self.rows[2]])

    def test_reverse_date_filters_are_rejected(self):
        with self.assertRaises(DeskError):
            between(self.rows, "day", "2025-04-01", "2025-03-31")

    def test_touching_ranges_overlap(self):
        self.assertTrue(overlaps("2025-03-28", "2025-03-31", "2025-03-31", "2025-04-01"))
        self.assertFalse(overlaps("2025-03-28", "2025-03-31", "2025-04-01", "2025-04-02"))
        self.assertEqual(duration("2025-03-31", "2025-04-02"), 2)

    def test_totals_order_and_csv_columns(self):
        self.assertEqual(
            list(totals(self.rows, "scope", "units").items()), [("north", 5), ("south", 4)]
        )
        self.assertEqual(csv_text(self.rows, ("id", "units")), "id,units\na,2\nb,4\nc,3\n")

    def test_window_excludes_cursor(self):
        feed = [{"seq": n} for n in (1, 3, 6, 7)]
        self.assertEqual(window(feed, after=3, limit=1), [feed[2]])
        self.assertEqual(window(feed, after=7, limit=3), [])
