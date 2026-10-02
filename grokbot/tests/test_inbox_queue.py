"""Queue integrity, ack identity, concurrent writes, corrupt tail, fsync faults."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import inbox_store as store  # noqa: E402
from inbox_store import (  # noqa: E402
    DurabilityError,
    ack_keys,
    append_record,
    claim_for_send,
    escalate_stale_sending,
    msg_key,
    parse_ack_argv,
    peek_claimable,
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
            n, missing = ack_keys(path, {("Caaa", ts)})
            self.assertEqual(n, 1)
            self.assertFalse(missing)
            und = peek_undelivered(path)
            self.assertEqual(len(und), 1)
            self.assertEqual(und[0]["channel"], "Cbbb")

    def test_ts_alone_must_not_ack_other_channel(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            ts = "1710000000.000200"
            append_record(path, _rec("C1", ts))
            append_record(path, _rec("C2", ts))
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
            parsed = [json.loads(l) for l in lines]
            keys = {msg_key(r) for r in parsed}
            self.assertEqual(len(keys), n)

    def test_concurrent_append_and_ack(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
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


class TestCorruptTail(unittest.TestCase):
    def test_truncated_tail_quarantined_new_append_readable(self):
        """Repro: truncated last line without newline must not swallow next msg."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            # Simulate crash mid-write: partial JSON, no trailing newline.
            partial = '{"channel":"Cold","ts":"1.0","text":"TRUNC'
            with open(path, "wb") as f:
                f.write(partial.encode())
            # New append must repair + succeed; peek must see the new message.
            ok = append_record(path, _rec("Cnew", "2.0", text="visible"))
            self.assertTrue(ok)
            und = peek_undelivered(path)
            keys = {msg_key(r) for r in und}
            self.assertIn(("Cnew", "2.0"), keys)
            # Corrupt evidence kept
            self.assertTrue(os.path.exists(path + ".corrupt"))
            with open(path + ".corrupt", "rb") as cf:
                blob = cf.read()
            self.assertIn(b"TRUNC", blob)
            # File lines are well-formed JSON
            with open(path, "rb") as f:
                data = f.read()
            self.assertTrue(data.endswith(b"\n"))
            for line in data.decode().splitlines():
                if line.strip():
                    json.loads(line)

    def test_append_does_not_report_success_if_verify_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            real_verify = store._verify_last_record

            def boom(*a, **k):
                raise DurabilityError("verify boom")

            with mock.patch.object(store, "_verify_last_record", side_effect=boom):
                with self.assertRaises(DurabilityError):
                    append_record(path, _rec("C", "1.0"))


class TestFsyncFaultInjection(unittest.TestCase):
    def test_append_fsync_failure_does_not_return_true(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            real_fsync = os.fsync
            calls = {"n": 0}

            def flaky(fd):
                calls["n"] += 1
                # Fail the first file fsync (append path)
                if calls["n"] == 1:
                    raise OSError(5, "EIO injected")
                return real_fsync(fd)

            with mock.patch("os.fsync", side_effect=flaky):
                with self.assertRaises(DurabilityError):
                    append_record(path, _rec("C", "1.0"))

    def test_duplicate_path_reconfirms_durability_before_false(self):
        """After a prior write, duplicate must fsync again; EIO → raise not False."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            self.assertTrue(append_record(path, _rec("C", "1.0")))

            with mock.patch.object(
                store, "fsync_file_and_dir",
                side_effect=DurabilityError("dir EIO"),
            ):
                with self.assertRaises(DurabilityError):
                    append_record(path, _rec("C", "1.0", text="dup"))

    def test_dir_fsync_eio_not_swallowed_on_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            append_record(path, _rec("C", "1.0"))
            real_fsync_dir = store._fsync_dir

            def boom(p):
                raise OSError(5, "EIO dir")

            with mock.patch.object(store, "_fsync_dir", side_effect=boom):
                with self.assertRaises(DurabilityError):
                    set_reply_status(path, {("C", "1.0")}, "sent")


class TestClaimBeforeSend(unittest.TestCase):
    def test_claim_atomic_second_loses(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            append_record(path, _rec("C", "1.0"))
            self.assertTrue(claim_for_send(path, ("C", "1.0"), "tok1"))
            self.assertFalse(claim_for_send(path, ("C", "1.0"), "tok2"))
            und = peek_undelivered(path)[0]
            self.assertEqual(und["reply_status"], "sending")
            self.assertEqual(und["claim_id"], "tok1")
            self.assertEqual(peek_claimable(path), [])

    def test_stale_sending_escalates_to_uncertain(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            append_record(path, _rec("C", "1.0"))
            claim_for_send(path, ("C", "1.0"))
            n = escalate_stale_sending(path)
            self.assertEqual(n, 1)
            self.assertEqual(peek_undelivered(path)[0]["reply_status"], "uncertain")


class TestReplyStatusSplit(unittest.TestCase):
    def test_sent_then_ack_failure_leaves_sent_undelivered(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            append_record(path, _rec("C", "1.0"))
            set_reply_status(path, {("C", "1.0")}, "sent")
            und = peek_undelivered(path)
            self.assertEqual(len(und), 1)
            self.assertEqual(und[0]["reply_status"], "sent")
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


class TestCorruptTailAtomicRepair(unittest.TestCase):
    def test_repair_write_failure_preserves_original_queue(self):
        """Mid-repair failure must not truncate away prior good records."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            # Two good lines + truncated tail (no final newline)
            good1 = json.dumps(_rec("C", "1.0", text="keep-a"), ensure_ascii=False)
            good2 = json.dumps(_rec("C", "2.0", text="keep-b"), ensure_ascii=False)
            partial = '{"channel":"C","ts":"3.0","text":"TRUNC'
            with open(path, "wb") as f:
                f.write((good1 + "\n" + good2 + "\n" + partial).encode())
            with open(path, "rb") as rf:
                original = rf.read()

            real_replace = os.replace

            def boom_replace(src, dst):
                raise OSError(5, "EIO replace injected")

            with mock.patch("os.replace", side_effect=boom_replace):
                with self.assertRaises(store.DurabilityError):
                    store.read_records_repair_tail(path)
            # Original bytes still present (not truncated by wb open)
            with open(path, "rb") as rf:
                after = rf.read()
            self.assertEqual(after, original)
            self.assertIn(b"keep-a", after)
            self.assertIn(b"keep-b", after)
            # Corrupt evidence still written
            self.assertTrue(os.path.exists(path + ".corrupt"))
            with open(path + ".corrupt", "rb") as cf:
                self.assertIn(b"TRUNC", cf.read())

    def test_repair_uses_temp_then_replace_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            good = json.dumps(_rec("C", "1.0", text="keep"), ensure_ascii=False)
            with open(path, "wb") as f:
                f.write((good + "\n" + '{"trunc').encode())
            rows, repaired = store.read_records_repair_tail(path)
            self.assertTrue(repaired)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["text"], "keep")
            with open(path, "rb") as rf:
                data = rf.read()
            self.assertTrue(data.endswith(b"\n"))
            self.assertNotIn(b'{"trunc', data)
            with open(path + ".corrupt", "rb") as cf:
                self.assertIn(b"trunc", cf.read())


class TestAckDurabilityConfirm(unittest.TestCase):
    def test_idempotent_ack_still_fsyncs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            append_record(path, _rec("C", "1.0"))
            ack_keys(path, {("C", "1.0")})  # dirty rewrite
            # Second ack: dirty=False but must still confirm durability
            with mock.patch.object(
                store, "fsync_file_and_dir",
                side_effect=store.DurabilityError("dir EIO again"),
            ):
                with self.assertRaises(store.DurabilityError):
                    ack_keys(path, {("C", "1.0")})
