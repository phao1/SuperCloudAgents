import unittest

from stockroom import fields
from stockroom.errors import StockroomError
from stockroom.query import Conditions


class ValidationTests(unittest.TestCase):
    def test_codes_respect_ascii_and_length_and_do_not_strip(self):
        self.assertEqual(fields.code("a-2", "sku"), "A-2")
        self.assertEqual(fields.code("1" * 24, "sku"), "1" * 24)
        for value in ("", "a" * 25, "-bad", " a", "a\n", "straße", "ı", None, 0):
            with self.subTest(value=value), self.assertRaisesRegex(StockroomError, "invalid sku"):
                fields.code(value, "sku")

    def test_optional_fields_keep_none_as_sentinel(self):
        self.assertIsNone(fields.optional_day(None))
        self.assertIsNone(fields.optional_code(None, "lot"))
        self.assertEqual(fields.optional_code("lot", "lot"), "LOT")
        with self.assertRaisesRegex(StockroomError, "invalid day"):
            fields.optional_day("")
        with self.assertRaisesRegex(StockroomError, "invalid lot"):
            fields.optional_code("", "lot")

    def test_only_calendar_dates_with_fixed_iso_shape_are_valid(self):
        self.assertEqual(fields.day("2000-02-29"), "2000-02-29")
        self.assertEqual(fields.day("0001-01-01"), "0001-01-01")
        for value in ("1900-02-29", "2024-2-9", "20240301", " 2024-03-01", "2024-03-01\n",
                      "2024-00-01", "2024-03-40", "２０２４-02-29"):
            with self.subTest(value=value), self.assertRaisesRegex(StockroomError, "invalid day"):
                fields.day(value)

    def test_ranges_accept_missing_and_equal_bounds(self):
        day = "2024-02-29"
        self.assertEqual(fields.order_days(None, day), (None, day))
        self.assertEqual(fields.order_days(day, None), (day, None))
        self.assertEqual(fields.order_days(day, day), (day, day))
        with self.assertRaisesRegex(StockroomError, "invalid day range"):
            fields.order_days(day, "2024-02-28")

    def test_quantities_follow_sqlite_integer_bounds(self):
        self.assertEqual(fields.positive(2**63 - 1), 2**63 - 1)
        self.assertEqual(fields.signed(-(2**63) + 1), -(2**63) + 1)
        for quantity in (0, True, False, 1.0, "1", 2**63):
            with self.subTest(value=quantity):
                with self.assertRaisesRegex(StockroomError, "invalid quantity"):
                    fields.positive(quantity)
                with self.assertRaisesRegex(StockroomError, "invalid adjustment"):
                    fields.signed(quantity)

    def test_title_and_memo_have_distinct_whitespace_rules(self):
        self.assertEqual(fields.title("\t Notes 雪 \n", "label"), "Notes 雪")
        self.assertEqual(fields.memo("\t Notes 雪 \n"), "\t Notes 雪 \n")
        self.assertEqual(fields.memo(""), "")
        with self.assertRaisesRegex(StockroomError, "invalid label"):
            fields.title("x" * 101, "label")
        with self.assertRaisesRegex(StockroomError, "invalid memo"):
            fields.memo("x" * 401)

    def test_predicates_bind_values_in_the_same_order_as_clauses(self):
        conditions = Conditions()
        self.assertEqual(conditions.where(), "")
        conditions.equals("sku", None)
        conditions.equals("warehouse", "NORTH")
        conditions.compare("received", ">=", "2024-02-29")
        conditions.equals("quantity", 0)
        self.assertEqual(conditions.where(), " WHERE warehouse = ? AND received >= ? AND quantity = ?")
        self.assertEqual(conditions.arguments, ["NORTH", "2024-02-29", 0])
