"""Queue integrity, ack identity, concurrent writes — stdlib unittest."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from inbox_store import (  # noqa: E402
    ack_keys,
    append_record,
    msg_key,
    parse_ack_argv,
    peek_undelivered,
    set_reply_status,
)


def _rec(channel, ts, text="hi", **extra):
    r = {
        "channel": channel,
        "ts": ts,
        "text": text,
        "user": "U1",
        "delivered": False,
        "reply_status": None,
    }
    r.update(extra)
    return r


class TestAckIdentity(unittest.TestCase):
    def test_parse_pairs_and_colon(self):
        self.assertEqual(
            parse_ack_argv(["C1", "1.0", "C2", "2.0"]),
            {("C1", "1.0"), ("C2", "2.0")},
        )
        self.assertEqual(
            parse_ack_argv(["C1:1.0", "C2:2.0"]),
            {("C1", "1.0"), ("C2", "2.0")},
        )

    def test_same_ts_different_channels(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            ts = "1710000000.000100"
            self.assertTrue(append_record(path, _rec("Caaa", ts, "a")))
            self.assertTrue(append_record(path, _rec("Cbbb", ts, "b")))
            # Ack only one channel
            n, missing = ack_keys(path, {("Caaa", ts)})
            self.assertEqual(n, 1)
            self.assertFalse(missing)
            und = peek_undelivered(path)
            self.assertEqual(len(und), 1)
            self.assertEqual(und[0]["channel"], "Cbbb")
            self.assertEqual(und[0]["ts"], ts)

    def test_ts_alone_must_not_ack_other_channel(self):
        """Regression: old CLI keyed only on ts would mark both channels."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            ts = "1710000000.000200"
            append_record(path, _rec("C1", ts))
            append_record(path, _rec("C2", ts))
            # If we mistakenly keyed on ts alone, both would flip. We don't.
            n, missing = ack_keys(path, {("C1", ts)})
            self.assertEqual(n, 1)
            rows = peek_undelivered(path)
            self.assertEqual([msg_key(r) for r in rows], [("C2", ts)])


class TestDurableAckRewrite(unittest.TestCase):
    def test_ack_uses_temp_rename_file_intact(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            for i in range(5):
                append_record(path, _rec("C", f"1.{i}", text=str(i)))
            ack_keys(path, {("C", "1.2")})
            with open(path) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(lines), 5)
            delivered = [r for r in lines if r["delivered"]]
            self.assertEqual(len(delivered), 1)
            self.assertEqual(delivered[0]["ts"], "1.2")


class TestConcurrentWrites(unittest.TestCase):
    def test_concurrent_appends_no_lost_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            n = 40

            def worker(i):
                return append_record(path, _rec(f"C{i % 3}", f"9.{i:04d}", text=str(i)))

            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(worker, range(n)))
            self.assertTrue(all(results))
            with open(path) as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), n)
            # Valid JSON each line
            parsed = [json.loads(l) for l in lines]
            keys = {msg_key(r) for r in parsed}
            self.assertEqual(len(keys), n)

    def test_concurrent_append_and_ack(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            # Seed
            for i in range(10):
                append_record(path, _rec("Cseed", f"0.{i:03d}"))

            errors = []

            def appender(i):
                try:
                    append_record(path, _rec("Cnew", f"1.{i:03d}"))
                except Exception as e:
                    errors.append(e)

            def acker(i):
                try:
                    ack_keys(path, {("Cseed", f"0.{i:03d}")})
                except Exception as e:
                    errors.append(e)

            threads = []
            for i in range(10):
                threads.append(threading.Thread(target=appender, args=(i,)))
                threads.append(threading.Thread(target=acker, args=(i,)))
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertFalse(errors)
            with open(path) as f:
                rows = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(rows), 20)
            seed_undelivered = [
                r for r in rows
                if r["channel"] == "Cseed" and not r.get("delivered")
            ]
            self.assertEqual(seed_undelivered, [])


class TestReplyStatusSplit(unittest.TestCase):
    def test_sent_then_ack_failure_leaves_sent_undelivered(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            append_record(path, _rec("C", "1.0"))
            set_reply_status(path, {("C", "1.0")}, "sent")
            und = peek_undelivered(path)
            self.assertEqual(len(und), 1)
            self.assertEqual(und[0]["reply_status"], "sent")
            self.assertFalse(und[0]["delivered"])
            # Simulate second round: only ack
            ack_keys(path, {("C", "1.0")})
            self.assertEqual(peek_undelivered(path), [])

    def test_uncertain_stays_undelivered(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            append_record(path, _rec("C", "2.0"))
            set_reply_status(path, {("C", "2.0")}, "uncertain")
            und = peek_undelivered(path)
            self.assertEqual(len(und), 1)
            self.assertEqual(und[0]["reply_status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
