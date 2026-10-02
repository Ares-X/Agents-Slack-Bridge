"""Isolation tests for inbox_store: concurrency, identity, ack semantics.

Run:  python -m unittest discover -s tests -v   (from muse/)
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inbox_store


def rec(channel, ts, text="hi"):
    return {
        "msg_id": inbox_store.msg_id(channel, ts),
        "channel": channel,
        "user": "U1",
        "text": text,
        "kind": "mention",
        "ts": ts,
        "thread_ts": "",
        "received_at": 0.0,
    }


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inbox_test_")
        # Redirect module paths (looked up at call time, so patchable).
        inbox_store.INBOX_PATH = os.path.join(self.tmp, "inbox.jsonl")
        inbox_store.LOCK_PATH = os.path.join(self.tmp, "inbox.lock")

    def raw_lines(self):
        with open(inbox_store.INBOX_PATH) as f:
            return f.readlines()

    def test_concurrent_append_no_loss_no_dup(self):
        """8 threads x 25 appends incl. duplicates -> all 200 unique kept once."""
        errors = []

        def worker(n):
            try:
                for i in range(25):
                    # every 5th record is a deliberate duplicate redelivery
                    ts = "1.%03d" % (i if i % 5 else 0)
                    inbox_store.append_record(rec("C1", ts))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        got = inbox_store.read_undelivered()
        # unique ts values: 0,1,2,3,4,6,7,...,24 -> 1 + 20 = 21
        self.assertEqual(len(got), 21)
        # every line is valid JSON (no torn writes)
        for line in self.raw_lines():
            json.loads(line)

    def test_cross_channel_same_ts(self):
        """Same ts in two channels are distinct identities; ack one only."""
        self.assertTrue(inbox_store.append_record(rec("CA", "9.9")))
        self.assertTrue(inbox_store.append_record(rec("CB", "9.9")))
        inbox_store.ack([inbox_store.msg_id("CA", "9.9")])
        rest = inbox_store.read_undelivered()
        self.assertEqual([m["msg_id"] for m in rest], ["CB:9.9"])

    def test_redelivery_after_ack_not_requeued(self):
        """Slack redelivery of an already-acked event is dropped silently."""
        self.assertTrue(inbox_store.append_record(rec("C1", "5.5")))
        inbox_store.ack([inbox_store.msg_id("C1", "5.5")])
        self.assertFalse(inbox_store.append_record(rec("C1", "5.5")))
        self.assertEqual(inbox_store.read_undelivered(), [])

    def test_ack_never_rewrites_hot_path(self):
        """Ack appends tombstones; original records stay byte-identical."""
        inbox_store.append_record(rec("C1", "1.1", text="hello"))
        before = self.raw_lines()
        inbox_store.ack([inbox_store.msg_id("C1", "1.1")])
        after = self.raw_lines()
        self.assertEqual(after[0], before[0])  # original line untouched
        self.assertIn('"type": "ack"', after[1])

    def test_compact_drops_acked(self):
        inbox_store.append_record(rec("C1", "1.1"))
        inbox_store.append_record(rec("C1", "2.2"))
        inbox_store.ack([inbox_store.msg_id("C1", "1.1")])
        stats = inbox_store.compact()
        self.assertEqual(stats, {"kept": 1, "dropped": 1})
        rest = inbox_store.read_undelivered()
        self.assertEqual([m["msg_id"] for m in rest], ["C1:2.2"])

    def test_msg_id_format(self):
        self.assertEqual(inbox_store.msg_id("CABC", "123.456"), "CABC:123.456")


if __name__ == "__main__":
    unittest.main()
