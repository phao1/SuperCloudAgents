from dataclasses import replace
from unittest import TestCase

from harborrelay.encoding import as_bytes, decode, envelope, row_bytes, validate
from harborrelay.errors import RelayError

from tests.support import event, notice


class EncodingTests(TestCase):
    def test_wire_roundtrip_does_not_normalize_operator_text(self):
        value = {"memo": 'Zürich, 航運\t"check"\n', "names": ["é", "e\u0301"]}
        raw = row_bytes(event(52, **value))
        self.assertIn("航運".encode(), raw)
        self.assertIn(b'\\n', raw)
        self.assertEqual(envelope(decode(raw)), event(52, **value))

    def test_decoder_rejects_duplicate_fields_recursively(self):
        for data in ('{"kind":"a","kind":"b"}', '{"note":{"x":1,"x":2}}'):
            with self.subTest(data=data), self.assertRaisesRegex(RelayError, "invalid JSON value"):
                decode(data)

    def test_invalid_utf8_and_nonfinite_constants_are_reported(self):
        cases = [b'{"label":"\xff"}', "[NaN]", '{"memo":Infinity}', "-Infinity"]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(RelayError):
                decode(raw)

    def test_encoder_rejects_nonfinite_and_unencodable_objects(self):
        for value in [float("inf"), {"val": object()}, {"bad": "\udc80"}, {1, 2}]:
            with self.subTest(value=repr(value)), self.assertRaises(RelayError):
                as_bytes(value)

    def test_notice_payload_is_an_object(self):
        original = notice(a=[2, {"note": None}])
        self.assertEqual(validate(original), original)
        for wrong in ([], None, True, 7, "payload"):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(RelayError, "invalid payload"):
                validate(replace(original, value=wrong))

    def test_invalid_identity_fields(self):
        for fields, reason in [
            ({"key": ""}, "key"), ({"key": "a\0b"}, "key"), ({"key": "x" * 101}, "key"),
            ({"stream": "a.*"}, "stream"), ({"kind": "No space"}, "kind"),
            ({"stamp": -1}, "stamp"), ({"stamp": True}, "stamp"),
        ]:
            with self.subTest(fields=fields), self.assertRaisesRegex(RelayError, f"invalid {reason}"):
                validate(replace(notice(), **fields))

    def test_record_rejects_extra_and_missing_fields(self):
        raw = decode(row_bytes(event(1)))
        for wrong in (None, {"seq": 1}, {**raw, "unexpected": True}):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(RelayError, "invalid journal record"):
                envelope(wrong)
