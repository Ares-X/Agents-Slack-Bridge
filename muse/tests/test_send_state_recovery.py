"""Durable completion and rate-limit recovery, using isolated queue files."""
import builtins
import errno
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "consumer")]
import inbox_store
import poll_consumer as pc
from send_state import SendState, StateCorruptError


class DurableRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.path = os.path.join(self.tmp.name, "send_state.json")
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        self.lock = os.path.join(self.tmp.name, "inbox.lock")
        for key, value in {"INBOX_PATH": self.inbox, "LOCK_PATH": self.lock,
                           "_MIGRATED": {}}.items():
            self.stack.enter_context(patch.object(inbox_store, key, value))
        self.message = {"msg_id": "C1:1", "channel": "C1", "ts": "1",
                        "text": "hello", "thread_ts": "", "user": "U1"}
        inbox_store.append_record(self.message)

    def state(self):
        return SendState(self.path, self.inbox, self.lock)

    def claim(self, state, attempt="attempt-1"):
        return state.claim("C1:1", "C1", "", "hash", client_msg_id=attempt)

    def test_unreadable_completion_does_not_resend_stale_snapshot(self):
        a, b = self.state(), self.state()
        snapshot = inbox_store.read_undelivered()[0]

        def ack(ids):
            inbox_store.ack(ids)
            return True

        with patch.object(pc, "channel_history", return_value=([], None)), \
             patch.object(pc, "generate_reply", return_value="reply"), \
             patch.object(pc, "send_reply", return_value="ok") as send, \
             patch.object(pc, "ack", side_effect=ack):
            self.assertEqual(pc.handle_one(snapshot, {}, a), "replied")
            real_open = builtins.open
            for error in (OSError(errno.EIO, "read failed"),
                          FileNotFoundError(errno.ENOENT, "missing queue")):
                def unreadable(path, mode="r", *args, **kwargs):
                    if path == self.inbox and mode == "r":
                        raise error
                    return real_open(path, mode, *args, **kwargs)
                with self.subTest(error=type(error).__name__), \
                     patch("builtins.open", side_effect=unreadable):
                    with self.assertRaises(OSError):
                        pc.handle_one(snapshot, {}, b)
                self.assertEqual(send.call_count, 1)
            self.assertEqual(pc.handle_one(snapshot, {}, b), "already-acked")
            self.assertEqual(send.call_count, 1)

    def test_deadline_survives_restart_and_expiry_claim_is_exclusive(self):
        with patch("time.time", return_value=1000):
            a = self.state()
            self.claim(a)
            self.assertTrue(a.defer_retry("C1:1", 1120, "attempt-1"))
        with patch("time.time", return_value=1119):
            a, b = self.state(), self.state()
            self.assertEqual(a.get("C1:1")["retry_at"], 1120)
            self.assertEqual(self.claim(a, "too-early")[1], "held")
        with patch("time.time", return_value=1120), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: self.claim(*pair),
                                    [(a, "new-a"), (b, "new-b")]))
        self.assertCountEqual([r[1] for r in results], ["claimed", "held"])
        self.assertEqual(a.get("C1:1")["status"], "sending")
        self.assertIn(a.get("C1:1")["client_msg_id"], ("new-a", "new-b"))

    def test_late_verifier_cannot_replace_wait_or_new_attempt(self):
        with patch("time.time", return_value=1000):
            state = self.state()
            self.claim(state)
            state.defer_retry("C1:1", 1120, "attempt-1")
            state.set_uncertain("C1:1", 1, expected_client_msg_id="attempt-1")
            self.assertEqual(state.get("C1:1")["status"], "retry_wait")
        with patch("time.time", return_value=1120):
            self.claim(state, "attempt-2")
            state.set_uncertain("C1:1", 2, expected_client_msg_id="attempt-1")
            self.assertFalse(state.defer_retry("C1:1", 1240, "attempt-1"))
            self.assertEqual(state.get("C1:1")["status"], "sending")
            self.assertEqual(state.get("C1:1")["client_msg_id"], "attempt-2")

    def test_failed_deadline_save_does_not_release_claim(self):
        state = self.state()
        self.claim(state)
        with patch.object(state, "_save_locked", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                state.defer_retry("C1:1", 1120, "attempt-1")
        reopened = self.state()
        self.assertEqual(reopened.get("C1:1")["status"], "uncertain")
        self.assertEqual(self.claim(reopened, "attempt-2")[1], "held")

    def test_corrupt_deadlines_fail_closed(self):
        for deadline in (None, True, "1120", -1, float("nan"), float("inf")):
            with self.subTest(deadline=deadline):
                with open(self.path, "w") as f:
                    json.dump({"v": 2, "sends": {"C1:1": {
                        "status": "retry_wait", "retry_at": deadline}}}, f)
                with self.assertRaises(StateCorruptError):
                    self.state()


if __name__ == "__main__":
    unittest.main()
