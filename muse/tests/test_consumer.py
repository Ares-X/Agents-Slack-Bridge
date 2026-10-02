"""Isolation tests for consumer: mention hygiene, ack/send state machine,
history-degradation policy.

Run:  python -m unittest discover -s tests -v   (from muse/consumer/.. i.e. muse/)
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "consumer"))

import poll_consumer as pc


def msg(mid="C1:1.1", text="hi <@U999>"):
    ch, ts = mid.split(":")
    return {"msg_id": mid, "channel": ch, "user": "U1", "text": text,
            "kind": "mention", "ts": ts, "thread_ts": ""}


class MentionHygieneTest(unittest.TestCase):
    def test_echo_strips_real_mention(self):
        self.assertEqual(pc.strip_mentions("hi <@U123ABC>"), "hi @U123ABC")
        # angle brackets gone -> Slack will NOT notify
        self.assertNotIn("<@", pc.strip_mentions("hi <@U123ABC>"))

    def test_channel_and_special_mentions(self):
        self.assertEqual(pc.strip_mentions("<#C123|general>!"), "#general!")
        self.assertEqual(pc.strip_mentions("<!channel> look"), "@channel look")

    def test_normalize_reply(self):
        self.assertEqual(pc.normalize_reply("hello"), ("hello", []))
        self.assertEqual(pc.normalize_reply(("hello", ["U1", "U2"])),
                         ("hello", ["U1", "U2"]))


class SendStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.sessions = {}
        self.state = {}
        self.sent_texts = []
        self.ack_calls = []
        self.ack_results = [True]

    def fake_sh(self, *args, **kwargs):
        # emulate send.py
        class R:
            returncode = 0
            stdout = "sent ok: True ts: 9.9"
            stderr = ""
        self.sent_texts.append((args, kwargs.get("input_text")))
        return R()

    def fake_ack(self, mids):
        self.ack_calls.append(list(mids))
        return self.ack_results.pop(0) if self.ack_results else True

    def run_one(self, m, hist=None):
        hist = hist if hist is not None else ([], None)
        with patch.object(pc, "sh", self.fake_sh), \
             patch.object(pc, "ack", self.fake_ack), \
             patch.object(pc, "channel_history", return_value=hist), \
             patch.object(pc, "generate_reply", return_value="reply!"), \
             patch.object(pc, "verify_sent", return_value=False):
            return pc.handle_one(m, self.sessions, self.state)

    def test_ok_path_acks(self):
        self.assertEqual(self.run_one(msg()), "replied")
        self.assertEqual(len(self.sent_texts), 1)
        self.assertEqual(self.ack_calls, [["C1:1.1"]])
        self.assertNotIn("sent_unacked", self.state)

    def test_ack_failure_does_not_resend(self):
        self.ack_results = [False]
        self.assertEqual(self.run_one(msg()), "sent-unacked")
        self.assertEqual(len(self.sent_texts), 1)
        # next round: only ack retried, no resend
        self.ack_results = [True]
        with patch.object(pc, "sh", self.fake_sh), \
             patch.object(pc, "ack", self.fake_ack), \
             patch.object(pc, "channel_history", return_value=([], None)), \
             patch.object(pc, "generate_reply", return_value="reply!"), \
             patch.object(pc, "verify_sent", return_value=False):
            self.assertEqual(
                pc.handle_one(msg(), self.sessions, self.state), "acked-later")
        self.assertEqual(len(self.sent_texts), 1)  # still exactly one send

    def test_explicit_mentions_use_flag(self):
        with patch.object(pc, "sh", self.fake_sh), \
             patch.object(pc, "ack", self.fake_ack), \
             patch.object(pc, "channel_history", return_value=([], None)), \
             patch.object(pc, "generate_reply",
                          return_value=("hello", ["U9"])), \
             patch.object(pc, "verify_sent", return_value=False):
            pc.handle_one(msg(), self.sessions, self.state)
        args, _ = self.sent_texts[0]
        self.assertIn("--mention", args)
        self.assertIn("U9", args)


class UncertainSendTest(unittest.TestCase):
    def test_uncertain_does_not_blind_resend(self):
        sessions, state = {}, {}
        m = msg()
        sends = []

        def flaky_sh(*args, **kwargs):
            if "send.py" in args:
                sends.append(1)
                raise TimeoutError("hung")
            class R:
                returncode = 0
                stdout = ""
            return R()

        with patch.object(pc, "sh", flaky_sh), \
             patch.object(pc, "ack") as mack, \
             patch.object(pc, "channel_history", return_value=([], None)), \
             patch.object(pc, "generate_reply", return_value="reply!"), \
             patch.object(pc, "verify_sent", return_value=False):
            res = pc.handle_one(m, sessions, state)
        self.assertEqual(res, "uncertain-held")
        self.assertEqual(len(sends), 1)
        mack.assert_not_called()  # never acked an unverified send
        # second round: still no resend, verification attempted instead
        with patch.object(pc, "sh", flaky_sh), \
             patch.object(pc, "ack") as mack2, \
             patch.object(pc, "channel_history", return_value=([], None)), \
             patch.object(pc, "generate_reply", return_value="reply!"), \
             patch.object(pc, "verify_sent", return_value=None) as mv:
            res2 = pc.handle_one(m, sessions, state)
        self.assertEqual(len(sends), 1)
        self.assertTrue(mv.called)
        mack2.assert_not_called()
        self.assertEqual(res2, "uncertain-held")

    def test_uncertain_verified_then_acked(self):
        sessions, state = {}, {}
        m = msg()
        with patch.object(pc, "sh", side_effect=TimeoutError("hung")), \
             patch.object(pc, "channel_history", return_value=([], None)), \
             patch.object(pc, "generate_reply", return_value="reply!"), \
             patch.object(pc, "verify_sent", return_value=True), \
             patch.object(pc, "ack", return_value=True) as mack:
            res = pc.handle_one(m, sessions, state)
        self.assertEqual(res, "verified-acked")
        mack.assert_called_once_with(["C1:1.1"])


class HistoryDegradationTest(unittest.TestCase):
    def test_history_error_defers_then_degrades(self):
        sessions, state = {}, {}
        m = msg()
        results = []
        for _ in range(4):
            with patch.object(pc, "sh") as msh, \
                 patch.object(pc, "ack", return_value=True), \
                 patch.object(pc, "channel_history",
                              return_value=([], "boom: timeout")), \
                 patch.object(pc, "generate_reply", return_value="reply!"), \
                 patch.object(pc, "verify_sent", return_value=False):
                msh.return_value.returncode = 0
                msh.return_value.stdout = "sent ok: True ts: 1"
                msh.return_value.stderr = ""
                results.append(pc.handle_one(m, sessions, state))
        # first two rounds defer (leave unacked), third proceeds degraded
        self.assertEqual(results[0], "deferred")
        self.assertEqual(results[1], "deferred")
        self.assertEqual(results[2], "replied")
        # degraded marker visible in session log, not in the Slack text
        sys_texts = [e["text"] for e in sessions["C1"] if e["role"] == "system"]
        self.assertTrue(any("degraded" in t and "boom" in t for t in sys_texts))


if __name__ == "__main__":
    unittest.main()
