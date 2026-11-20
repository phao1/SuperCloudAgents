from dataclasses import replace

from harborrelay.audit import archive, audit_pack, difference
from harborrelay.errors import ArchiveError, RelayError
from harborrelay.wire import MAGIC, encode_pack

from tests.support import DeskTest, event, notice


class AuditTests(DeskTest):
    def archive(self, filename, rows):
        path = self.root / filename
        path.write_bytes(encode_pack(rows))
        return path

    def test_audit_reports_counts_by_sorted_stream_and_kind(self):
        rows = [
            replace(event(9, "dock.floor"), stamp=12),
            replace(event(4, "dock.floor"), stamp=1, kind="sent"),
            replace(event(5, "barn.floor"), stamp=32),
            replace(event(10, "dock.floor"), stamp=8),
        ]
        result = audit_pack(iter(rows))
        self.assertEqual((result.entries, result.unique_keys), (4, 2))
        self.assertEqual([row.stream for row in result.streams], ["barn.floor", "dock.floor"])
        north = result.streams[1]
        self.assertEqual((north.entries, north.first_stamp, north.last_stamp), (3, 1, 12))
        self.assertEqual(north.kinds, {"landed": 2, "sent": 1})
        self.assertEqual(list(north.kinds), ["landed", "sent"])

    def test_pack_inspection_does_not_open_or_create_the_journal(self):
        pack = self.archive("incoming.hr", [event(54)])
        metadata = self.json_cli("archive", "inspect", str(pack))
        self.assertEqual(metadata["entries"], 1)
        self.assertEqual(metadata["streams"][0]["stream"], "dock.arrivals")
        self.assertFalse(self.home.exists())

    def test_empty_pack_has_an_empty_audit(self):
        pack = self.root / "empty.hr"
        pack.write_bytes(MAGIC)
        self.assertEqual(self.json_cli("archive", "inspect", str(pack)),
                         {"entries": 0, "unique_keys": 0, "streams": []})

    def test_audit_rejects_a_corrupt_last_record(self):
        pack = self.archive("corrupt.hr", [event(7), event(5)])
        pack.write_bytes(pack.read_bytes()[:-1])
        with self.assertRaisesRegex(ArchiveError, "truncated archive frame"):
            audit_pack(archive(pack))
        self.cli_error("truncated archive frame", "archive", "inspect", str(pack))

    def test_parse_errors_leave_the_journal_untouched(self):
        kept = self.journal.append(notice())
        self.cli_error("cannot read missing.hr", "archive", "inspect", str(self.root / "missing.hr"))
        self.assertEqual(self.journal.all(), [kept])
        with self.assertRaisesRegex(RelayError, "cannot read missing.hr"):
            list(archive(self.root / "missing.hr"))

    def test_comparison_ignores_remote_sequences_and_object_field_order(self):
        first = event(1, meta={"a": [0, False], "b": ""}, other="й")
        retry = event(42, other="й", meta={"b": "", "a": [0, False]})
        self.assertEqual(difference([first], [retry]).matching, 1)
        reordered = event(43, other="й", meta={"b": "", "a": [False, 0]})
        diff = difference([first], [reordered])
        self.assertEqual((diff.matching, diff.only_left, diff.only_right), (0, [first], [reordered]))

    def test_comparison_matches_one_occurrence_and_preserves_remainder_order(self):
        copies = [event(i, memo="same") for i in (4, 2, 20)]
        unmatched = event(50, memo="left")
        left = [copies[0], unmatched, *copies[1:]]
        right = [copies[2], event(19, memo="right"), copies[1]]
        result = difference(iter(left), iter(right))
        self.assertEqual(result.matching, 2)
        self.assertEqual(result.only_left, [unmatched, copies[2]])
        self.assertEqual(result.only_right, [right[1]])

    def test_comparison_counts_different_kinds_and_times_independently(self):
        left = event(1)
        right = [replace(left, kind="sent"), replace(left, stamp=5)]
        report = difference([left], right)
        self.assertEqual((report.matching, report.only_left, report.only_right), (0, [left], right))

    def test_pack_diff_cli_can_compare_empty_and_nonempty(self):
        left = self.archive("left.hr", [])
        right = self.archive("right.hr", [event(77)])
        report = self.json_cli("archive", "diff", str(left), str(right))
        self.assertEqual(report["matching"], 0)
        self.assertEqual(report["only_left"], [])
        self.assertEqual([row["seq"] for row in report["only_right"]], [77])

    def test_pack_diff_cli_reads_both_inputs_before_printing(self):
        left = self.archive("left.hr", [event(1)])
        bad = self.root / "bad.hr"
        bad.write_bytes(b"invalid\n")
        self.cli_error("invalid archive header", "archive", "diff", str(left), str(bad))
