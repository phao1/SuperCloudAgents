from decimal import Decimal
from unittest import TestCase

from quayside import money, validation
from quayside.errors import LedgerError


class MoneyTests(TestCase):
    def test_amount_requires_exact_native_scale_and_allows_redundant_zeros(self):
        self.assertEqual(money.amount("1.0000", "JPY"), Decimal(1))
        self.assertEqual(money.amount("-1.125", "KWD"), Decimal("-1.125"))
        self.assertEqual(money.amount("0.10", "usd"), Decimal("0.10"))
        for value, currency in [("1.01", "JPY"), ("1.1234", "KWD"), ("-0.001", "USD")]:
            with self.subTest(value=value), self.assertRaisesRegex(LedgerError, "currency precision"):
                money.amount(value, currency)

    def test_lexical_decimal_rules_reject_float_exponent_and_sign_prefix(self):
        for value in (True, 1.0, Decimal(1), "1e2", " 1", "+1", "00.4", ".1", "١", "NaN"):
            with self.subTest(value=value), self.assertRaisesRegex(LedgerError, "invalid decimal amount"):
                money.amount(value, "USD")

    def test_currency_validation_uses_supported_iso_codes(self):
        self.assertEqual([money.currency(item) for item in ("uSd", "JPY", "kwd")],
                         ["USD", "JPY", "KWD"])
        for value in (None, "usd ", "US", "bitcoin"):
            with self.subTest(value=value), self.assertRaisesRegex(LedgerError, "unknown currency"):
                money.currency(value)

    def test_fixed_projection_keeps_trailing_zeros_but_not_negative_zero(self):
        for amount, code, expected in [
            ("1", "KWD", "1.000"), ("-0.000", "USD", "0.00"), ("-0.4", "JPY", "0"),
            ("-2.500", "JPY", "-3"), ("2.505", "CHF", "2.51"), ("0", "JPY", "0"),
        ]:
            with self.subTest(amount=amount, code=code):
                self.assertEqual(money.format_fixed(Decimal(amount), code), expected)

    def test_day_and_closed_window_are_strict(self):
        self.assertEqual(validation.window("2024-02-29", "2024-02-29"), ("2024-02-29", "2024-02-29"))
        for value in ("2025-02-29", "2024-7-03", "20240703", ""):
            with self.subTest(value=value), self.assertRaisesRegex(LedgerError, "invalid day"):
                validation.day(value)
        with self.assertRaisesRegex(LedgerError, "invalid date window"):
            validation.window("2024-07-03", "2024-07-02")

    def test_label_validation_keeps_outer_spaces_and_internal_unicode(self):
        value = " bâtiment, 臂 "
        self.assertEqual(validation.label(value, "reference"), value)
        for wrong in (None, "", " \t", "bad\0name", "A" * 301):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(LedgerError, "invalid reference"):
                validation.label(wrong, "reference")
