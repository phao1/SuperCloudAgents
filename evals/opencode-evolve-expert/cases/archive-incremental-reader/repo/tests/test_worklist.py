from unittest import TestCase
from unittest.mock import Mock

from harborrelay.errors import RelayError
from harborrelay.leases import Worklist

from tests.support import event


class WorklistTests(TestCase):
    def setUp(self):
        self.now = 10
        self.clock = Mock(side_effect=lambda: self.now)
        self.queue = Worklist(self.clock)

    def claim(self, owner="Alice", ttl=20):
        return self.queue.claim(owner, ttl)

    def test_claims_are_in_sequence_even_when_enqueued_out_of_order(self):
        for seq in (11, 5, 8):
            self.assertTrue(self.queue.enqueue(event(seq)))
        found = [self.claim().row.seq for _ in range(3)]
        self.assertEqual(found, [5, 8, 11])
        self.assertIsNone(self.claim())

    def test_expiry_equality_makes_a_claim_available(self):
        self.queue.enqueue(event(1))
        initial = self.claim(ttl=9)
        self.now = 18
        self.assertIsNone(self.claim())
        self.now = initial.lease.expires
        once = self.claim("Rima")
        self.assertEqual(once.lease.owner, "Rima")
        self.assertEqual(once.row.seq, initial.row.seq)

    def test_stale_ack_leaves_work_queued(self):
        self.queue.enqueue(event(1))
        initial = self.claim()
        self.now = 30
        with self.assertRaisesRegex(RelayError, "stale lease"):
            self.queue.ack(initial.lease)
        self.assertEqual(self.queue.pending(), [1])
        next_claim = self.claim("Rima")
        with self.assertRaisesRegex(RelayError, "stale lease"):
            self.queue.ack(initial.lease)
        self.queue.ack(next_claim.lease)
        self.assertEqual(self.queue.pending(), [])

    def test_extension_uses_current_clock_and_does_not_requeue(self):
        self.queue.enqueue(event(1))
        delivery = self.claim()
        self.now = 13
        extended = self.queue.extend(delivery.lease, 5)
        self.assertEqual((extended.seq, extended.owner, extended.expires), (1, "Alice", 18))
        self.now = 17
        self.assertIsNone(self.claim())
        self.queue.ack(extended)

    def test_release_leaves_the_original_event_available(self):
        incoming = event(1, notes=["wait for ramp"])
        self.queue.enqueue(incoming)
        delivery = self.claim()
        delivery.row.value["notes"].clear()
        self.queue.release(delivery.lease)
        again = self.claim("Rima")
        self.assertEqual(again.row, incoming)
        self.assertEqual(self.queue.pending(), [1])

    def test_acked_events_stay_done_after_duplicate_enqueue(self):
        row = event(2)
        self.assertTrue(self.queue.enqueue(row))
        self.assertFalse(self.queue.enqueue(row))
        self.queue.ack(self.claim().lease)
        self.assertFalse(self.queue.enqueue(row))
        self.assertIsNone(self.claim())

    def test_invalid_owner_and_ttl_do_not_call_the_clock(self):
        for owner, ttl, reason in [("_helper", 10, "owner"), ("Lou", True, "ttl"), ("Lou", 0, "ttl")]:
            with (
                self.subTest(owner=owner, ttl=ttl),
                self.assertRaisesRegex(RelayError, f"invalid {reason}"),
            ):
                self.claim(owner, ttl)
        self.clock.assert_not_called()
