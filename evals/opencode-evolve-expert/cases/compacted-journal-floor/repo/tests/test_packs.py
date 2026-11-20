from dataclasses import replace
from struct import pack
from zlib import crc32

from harborrelay.errors import ArchiveError, RelayError
from harborrelay.exchange import dump, load
from harborrelay.journal import Journal
from harborrelay.wire import MAGIC, MAX_BODY, encode_pack, iter_frames

from tests.support import DeskTest, event, notice


def frame(body):
    return pack(">I", len(body)) + body + pack(">I", crc32(body))


class ArchiveTests(DeskTest):
    def test_pack_uses_big_endian_length_and_raw_checksum(self):
        archive = encode_pack([event(800, memo="berth 🐟")])
        self.assertTrue(archive.startswith(MAGIC))
        body = archive[8:-4]
        self.assertEqual(int.from_bytes(archive[4:8]), len(body))
        self.assertEqual(int.from_bytes(archive[-4:]), crc32(body))
        self.assertIn("🐟".encode(), body)

    def test_pack_roundtrips_across_one_byte_chunks(self):
        rows = [event(7), event(9, "ca.dock", count=0), event(15, memo="更新")]
        wire = encode_pack(rows)
        self.assertEqual(list(iter_frames(bytes([byte]) for byte in wire)), rows)

    def test_header_only_is_an_empty_archive(self):
        self.assertEqual(encode_pack([]), MAGIC)
        self.assertEqual(list(iter_frames([MAGIC])), [])

    def test_decoding_reports_header_length_and_checksum_errors(self):
        valid = encode_pack([event(1)])
        cases = [
            (b"XX!\n", "invalid archive header"), (MAGIC[:2], "invalid archive header"),
            (MAGIC + pack(">I", 0), "invalid archive length"),
            (MAGIC + pack(">I", MAX_BODY + 1), "invalid archive length"),
            (valid[:-1] + bytes([valid[-1] ^ 1]), "archive checksum mismatch"),
            (valid[:-2], "truncated archive frame"), (MAGIC + b"\x00", "truncated archive frame"),
        ]
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ArchiveError, message):
                list(iter_frames([data]))

    def test_checksum_does_not_exempt_invalid_record_fields(self):
        for body in (b"{}", b'{"seq":1,"value":[]}', b'{"memo":"\xff"}', b"not JSON"):
            with self.subTest(body=body), self.assertRaisesRegex(ArchiveError, "invalid archive record"):
                list(iter_frames([MAGIC + frame(body)]))

    def test_encoder_rejects_oversized_payload(self):
        with self.assertRaisesRegex(ArchiveError, "invalid archive length"):
            encode_pack([event(17, memo="x" * MAX_BODY)])

    def test_import_reassigns_sequences_without_touching_timestamps(self):
        self.journal.append(notice(key="local"))
        pack_path = self.root / "in.hr"
        pack_path.write_bytes(encode_pack([replace(event(99), stamp=171), event(1)]))
        self.assertEqual([(row.seq, row.stamp) for row in load(self.journal, pack_path)],
                         [(2, 171), (3, 0)])
        self.assertEqual(Journal(self.home).head, 3)

    def test_import_is_atomic_after_late_checksum_failure(self):
        self.journal.append(notice())
        before = self.journal.all()
        path = self.root / "bad.hr"
        path.write_bytes(encode_pack([event(4), event(7)])[:-1])
        with self.assertRaisesRegex(ArchiveError, "truncated archive frame"):
            load(self.journal, path)
        self.assertEqual(self.journal.all(), before)
        self.assertEqual(Journal(self.home).all(), before)

    def test_dump_can_select_retained_sequence_range(self):
        self.journal.append_many([notice(stamp=3), notice(stamp=2), notice(stamp=1)])
        path = self.root / "out.hr"
        self.assertEqual(dump(self.journal, path, 1), 2)
        self.assertEqual([row.seq for row in iter_frames([path.read_bytes()])], [2, 3])
        self.assertEqual(self.journal.head, 3)

    def test_missing_input_file_has_an_operator_error(self):
        path = self.root / "missing.hr"
        with self.assertRaisesRegex(RelayError, "cannot read missing.hr"):
            load(self.journal, path)
        self.assertFalse(self.journal.path.exists())
