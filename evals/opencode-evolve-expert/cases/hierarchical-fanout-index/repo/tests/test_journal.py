from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch

from harborrelay.errors import RelayError
from harborrelay.journal import Journal
from harborrelay.types import Notice

from tests.support import DeskTest, notice


class JournalTests(DeskTest):
    def test_empty_read_does_not_initialize_a_directory(self):
        self.assertEqual(self.journal.head, 0)
        self.assertEqual(self.journal.all(), [])
        self.assertEqual(self.journal.read_after(0), [])
        self.assertFalse(self.home.exists())

    def test_sequence_ignores_source_timestamps(self):
        rows = self.journal.append_many([notice(stamp=25), notice(stamp=0), notice(stamp=11)])
        self.assertEqual([(row.seq, row.stamp) for row in rows], [(1, 25), (2, 0), (3, 11)])
        self.assertEqual(Journal(self.home).all(), rows)
        self.assertEqual(self.journal.head, 3)

    def test_append_returns_detached_snapshots(self):
        state = {"memo": ["ok"]}
        row = self.journal.append(Notice("south.gate", "ready", "J-01", state, 23))
        state["memo"].append("external")
        row.value["memo"].clear()
        result = self.journal.all()
        self.assertEqual(result[0].value, {"memo": ["ok"]})
        result[0].value.pop("memo")
        self.assertEqual(self.journal.read_after(0)[0].value, {"memo": ["ok"]})

    def test_append_is_atomic_when_a_late_notice_is_invalid(self):
        first = self.journal.append(notice())
        invalid = replace(notice(), stream="not..a.stream")
        with self.assertRaisesRegex(RelayError, "invalid stream"):
            self.journal.append_many([notice(), invalid])
        self.assertEqual(self.journal.all(), [first])
        self.assertEqual(Journal(self.home).all(), [first])
        self.assertEqual(self.journal.append(notice()).seq, 2)

    def test_save_failure_preserves_memory_and_the_file(self):
        first = self.journal.append(notice())
        with (
            patch("harborrelay.journal.replace_bytes", side_effect=RelayError("cannot write")),
            self.assertRaisesRegex(RelayError, "cannot write"),
        ):
            self.journal.append(notice())
        self.assertEqual(self.journal.head, 1)
        self.assertEqual(self.journal.all(), [first])
        self.assertEqual(Journal(self.home).all(), [first])

    def test_batches_are_contiguous_when_threads_share_a_journal(self):
        gate = Barrier(2, timeout=3)

        def append(name):
            gate.wait()
            return self.journal.append_many([notice(key=name), notice(key=name)])

        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = pool.submit(append, "left"), pool.submit(append, "right")
            groups = [left.result(timeout=4), right.result(timeout=4)]
        self.assertEqual(sorted([row.seq for row in group] for group in groups), [[1, 2], [3, 4]])
        self.assertEqual(Journal(self.home).all(), self.journal.all())

    def test_read_window_is_exclusive_and_supports_empty_results(self):
        self.journal.append_many(notice(key=str(index)) for index in range(8))
        self.assertEqual([row.seq for row in self.journal.read_after(3, 2)], [4, 5])
        self.assertEqual(self.journal.read_after(8), [])
        self.assertEqual(self.journal.read_after(999, 1), [])

    def test_invalid_cursor_and_limit_are_rejected_even_when_empty(self):
        for after, limit, reason in [(-1, 1, "sequence"), (False, 1, "sequence"),
                                     (0, 0, "limit"), (0, True, "limit")]:
            with (
                self.subTest(after=after, limit=limit),
                self.assertRaisesRegex(RelayError, f"invalid {reason}"),
            ):
                self.journal.read_after(after, limit)

    def test_prune_preserves_order_for_the_retained_suffix(self):
        self.journal.append_many([notice(stamp=8), notice(stamp=1), notice(stamp=3)])
        self.assertEqual(self.journal.prune(2), 2)
        self.assertEqual(self.journal.prune(2), 0)
        restored = Journal(self.home)
        self.assertEqual([row.seq for row in restored.all()], [3])
        self.assertEqual(restored.append(notice()).seq, 4)

    def test_invalid_journal_is_never_recreated(self):
        self.journal.append(notice())
        path = self.journal.path
        duplicates = path.read_bytes() * 2
        path.write_bytes(duplicates)
        with self.assertRaisesRegex(RelayError, "invalid journal file"):
            Journal(self.home)
        self.assertEqual(path.read_bytes(), duplicates)

    def test_empty_batch_never_writes_the_file(self):
        self.assertEqual(self.journal.append_many(iter(())), [])
        self.assertFalse(self.journal.path.exists())

    def test_corrupt_record_cannot_hide_behind_a_valid_record(self):
        self.journal.append(notice())
        content = self.journal.path.read_bytes() + b"{bad\n"
        self.journal.path.write_bytes(content)
        with self.assertRaisesRegex(RelayError, "invalid journal file"):
            Journal(self.home)
        self.assertEqual(self.journal.path.read_bytes(), content)
