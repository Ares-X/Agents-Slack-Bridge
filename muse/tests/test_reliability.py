"""Behavioral tests for the reliability review follow-ups.

Run:  python -m unittest discover -s tests -v   (from muse/)

Covers the six review findings:
  1. legacy inbox.jsonl (no msg_id, in-place delivered flag) upgrade
  2. send_reply: only PROVEN not-sent is "fail", everything else uncertain
  3. crash between durable claim and send -> never directly resendable
  4. storage failure: fsync errors raise; corrupt state never silent-empty
  5. verify_sent strict matching (own bot, channel/thread, attempt, exact text)
  6. torn tail quarantine + duplicate-branch durability confirmation
  7. compact keeps tombstones for the retention window; documented requeue
"""
import glob
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "consumer"))

import inbox_store
import poll_consumer as pc
from send_state import SendState, StateCorruptError


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


class StoreCase(unittest.TestCase):
    """Redirect inbox_store paths into a fresh tmpdir per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inbox_rel_test_")
        inbox_store.INBOX_PATH = os.path.join(self.tmp, "inbox.jsonl")
        inbox_store.LOCK_PATH = os.path.join(self.tmp, "inbox.lock")

    def raw(self):
        with open(inbox_store.INBOX_PATH, "rb") as f:
            return f.read()


class LegacyUpgradeTest(StoreCase):
    def legacy_rec(self, channel, ts, delivered, text="hi"):
        return {"channel": channel, "user": "U1", "text": text,
                "kind": "mention", "ts": ts, "thread_ts": "",
                "received_at": 0.0, "delivered": delivered}

    def write_legacy(self, records):
        with open(inbox_store.INBOX_PATH, "w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_pending_visible_done_not_replayed(self):
        self.write_legacy([
            self.legacy_rec("C1", "1.1", False),
            self.legacy_rec("C1", "2.2", False),
            self.legacy_rec("C1", "3.3", True),   # 已处理：绝不能重播
        ])
        got = inbox_store.read_undelivered()
        self.assertEqual([m["msg_id"] for m in got], ["C1:1.1", "C1:2.2"])

    def test_done_marker_wins_regardless_of_order(self):
        # 同一 identity 先出现 delivered=false、后出现 delivered=true：
        # 完成标记全局优先，已处理的消息绝不能被重播。
        self.write_legacy([
            self.legacy_rec("C1", "1.1", False),
            self.legacy_rec("C1", "1.1", True),
        ])
        got = inbox_store.read_undelivered()
        self.assertEqual(got, [])
        # 只有一个 tombstone，没有残留正文
        raw = self.raw().decode()
        self.assertEqual(raw.count('"type": "ack"'), 1)
        self.assertNotIn('"delivered": false', raw)

    def test_done_marker_wins_reversed_order(self):
        # 反过来（先 true 后 false）同样不能重播
        self.write_legacy([
            self.legacy_rec("C1", "1.1", True),
            self.legacy_rec("C1", "1.1", False),
        ])
        self.assertEqual(inbox_store.read_undelivered(), [])

    def test_migration_idempotent(self):
        self.write_legacy([
            self.legacy_rec("C1", "1.1", False),
            self.legacy_rec("C1", "3.3", True),
        ])
        first = inbox_store.read_undelivered()
        raw1 = self.raw()
        second = inbox_store.read_undelivered()
        raw2 = self.raw()
        self.assertEqual([m["msg_id"] for m in first],
                         [m["msg_id"] for m in second])
        self.assertEqual(raw1, raw2)  # 二次运行无变化
        # delivered=true 只产生一个 tombstone，没有重复
        tombstones = [l for l in raw2.decode().splitlines()
                      if '"type": "ack"' in l]
        self.assertEqual(len(tombstones), 1)

    def test_redelivery_of_migrated_done_is_deduped(self):
        self.write_legacy([self.legacy_rec("C1", "3.3", True)])
        inbox_store.read_undelivered()  # 触发迁移
        self.assertFalse(inbox_store.append_record(rec("C1", "3.3")))
        self.assertEqual(inbox_store.read_undelivered(), [])


class SendReplyUncertainTest(unittest.TestCase):
    def fake_result(self, returncode, stdout, stderr=""):
        msh = patch.object(pc, "sh").start()
        self.addCleanup(patch.stopall)
        msh.return_value.returncode = returncode
        msh.return_value.stdout = stdout
        msh.return_value.stderr = stderr
        return msh

    def test_nonzero_exit_without_proof_is_uncertain(self):
        # 复现：请求可能已被接受但响应丢失 -> 不能当成"未发送"自动重发
        self.fake_result(1, "", "SSLEOFError: connection reset")
        self.assertEqual(pc.send_reply("C1", "hi"), "uncertain")

    def test_timeout_is_uncertain(self):
        import subprocess
        with patch.object(pc, "sh", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(pc.send_reply("C1", "hi"), "uncertain")

    def test_result_not_sent_is_fail(self):
        self.fake_result(1, "", "RESULT not-sent: empty message")
        self.assertEqual(pc.send_reply("C1", "hi"), "fail")

    def test_api_reject_is_fail(self):
        self.fake_result(0, "sent ok: False ts: None")
        self.assertEqual(pc.send_reply("C1", "hi"), "fail")

    def test_sent_ok_true_is_ok(self):
        self.fake_result(0, "sent ok: True ts: 9.9")
        self.assertEqual(pc.send_reply("C1", "hi"), "ok")

    def test_ambiguous_output_is_uncertain(self):
        self.fake_result(0, "some unexpected output")
        self.assertEqual(pc.send_reply("C1", "hi"), "uncertain")


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sendstate_rel_test_")
        self.path = os.path.join(self.tmp, "send_state.json")

    def test_crash_after_claim_becomes_uncertain_never_sendable(self):
        st = SendState(self.path)
        claim, outcome = st.claim("C1:1.1", channel="C1", thread_ts="",
                                  text_hash="abc123")
        self.assertEqual(outcome, "claimed")
        self.assertEqual(claim["status"], "sending")
        # --- 模拟崩溃：丢掉实例，用新实例打开（= 新进程） ---
        del st
        st2 = SendState(self.path)
        entry = st2.get("C1:1.1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "uncertain")
        self.assertNotEqual(entry["status"], "sending")
        # handle_one 必须走核验分支，绝不能直接重发
        m = {"msg_id": "C1:1.1", "channel": "C1", "text": "hi",
             "thread_ts": ""}
        with patch.object(pc, "channel_history",
                           return_value=([], None)), \
             patch.object(pc, "send_reply") as msend, \
             patch.object(pc, "ack") as mack:
            res = pc.handle_one(m, {}, st2,
                                bot_id="B_OURS", bot_user_id="U_OURS")
        self.assertEqual(res, "uncertain-held")
        msend.assert_not_called()   # 没有第二次发送
        mack.assert_not_called()    # 未经核验不确认
        self.assertEqual(st2.get("C1:1.1")["status"], "uncertain")

    def test_concurrent_claim_second_does_not_own(self):
        # 两个 consumer 同时 claim：第二个拿不到发送权，只能核验不能发送
        st = SendState(self.path)
        entry1, outcome1 = st.claim("C1:9", channel="C1", thread_ts="",
                                    text_hash="h")
        self.assertEqual(outcome1, "claimed")
        entry2, outcome2 = st.claim("C1:9", channel="C1", thread_ts="",
                                    text_hash="h")
        self.assertEqual(outcome2, "held")
        self.assertEqual(entry2["status"], "sending")

    def test_sending_entry_goes_to_verify_not_resend(self):
        # 残留 sending 条目（并发或上轮崩溃）：handle_one 走核验不重发
        st = SendState(self.path)
        st.claim("C1:7", channel="C1", thread_ts="", text_hash="abc")
        m = {"msg_id": "C1:7", "channel": "C1", "text": "hi",
             "thread_ts": ""}
        with patch.object(pc, "channel_history",
                           return_value=([], None)), \
             patch.object(pc, "send_reply") as msend:
            res = pc.handle_one(m, {}, st,
                                bot_id="B_OURS", bot_user_id="U_OURS")
        self.assertEqual(res, "uncertain-held")
        msend.assert_not_called()
        self.assertEqual(st.get("C1:7")["status"], "uncertain")

    def test_wire_text_hash_covers_mentions(self):
        # send.py 会把 --mention 追加到正文末尾：hash 必须对最终正文算
        self.assertEqual(pc.wire_text("hello", ["U1", "U2"]),
                         "hello <@U1> <@U2>")
        self.assertEqual(pc.wire_text("hello  ", []), "hello  ")
        wire = pc.wire_text("hi", ["U9"])
        self.assertEqual(pc.text_hash(wire),
                         hashlib.sha256(wire.encode("utf-8")).hexdigest())
        # 与 send.py 的拼接规则一致（两侧改一处必须改另一处）
        import re
        src = open(os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))),
            "send.py")).read()
        self.assertRegex(
            src, r"text\.rstrip\(\) \+ \" \" \+ \" \"\.join\(f\"<@\{u\}>\"")
        with open(self.path, "w") as f:
            json.dump({"sent_unacked": ["C1:1"],
                       "uncertain": {"C1:2": {"attempts": 1, "last": 0.0,
                                              "text": "hi"}},
                       "hist_deferred": {}}, f)
        st = SendState(self.path)
        self.assertEqual(st.get("C1:1")["status"], "unacked")
        e = st.get("C1:2")
        self.assertEqual(e["status"], "uncertain")
        # v1 没有全文哈希：永远无法自动确认，只能等人工
        self.assertIsNone(e["text_hash"])


class StorageFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sendstate_rel_test_")
        self.path = os.path.join(self.tmp, "send_state.json")

    def test_fsync_failure_raises_not_silent(self):
        st = SendState(self.path)
        with patch("os.fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                st.claim("C1:1", channel="C1", thread_ts="", text_hash="h")
        # 事后仍可构造（不抛 StateCorruptError）：没有静默损坏
        st2 = SendState(self.path)
        self.assertIsNotNone(st2)

    def test_corrupt_primary_with_only_stale_bak_fails_closed(self):
        # P1-1: primary 损坏后只剩旧 .bak（领取前的版本）时，绝不能静默
        # 恢复——那会让可能已发送的消息重新变得可直接发送。
        st = SendState(self.path)
        st.claim("C1:1", channel="C1", thread_ts="", text_hash="h")
        st.set_unacked("C1:1")   # 触发 .bak 轮转
        self.assertTrue(os.path.exists(self.path + ".bak"))
        with open(self.path, "w") as f:
            f.write("garbage{{{ not json")
        with self.assertRaises(StateCorruptError):
            SendState(self.path)
        # 损坏证据按来源分别保留，不互相覆盖
        corrupt = glob.glob(self.path + ".corrupt.*")
        tags = {p.rsplit(".", 1)[-1] for p in corrupt}
        self.assertIn("primary", tags)
        self.assertIn("bak", tags)
        # 隔离标记存在：再次构造仍然 raise，绝不静默变空
        self.assertTrue(os.path.exists(self.path + ".quarantined"))
        with self.assertRaises(StateCorruptError):
            SendState(self.path)

    def test_total_corruption_quarantines_and_raises(self):
        st = SendState(self.path)
        st.claim("C1:1", channel="C1", thread_ts="", text_hash="h")
        for p in (self.path, self.path + ".tmp", self.path + ".bak"):
            with open(p, "w") as f:
                f.write("garbage{{{")
        with self.assertRaises(StateCorruptError):
            SendState(self.path)
        # 损坏证据保留
        corrupt = glob.glob(self.path + ".corrupt.*")
        self.assertTrue(corrupt)
        # 隔离标记存在：再次构造仍然 raise，绝不静默变空
        self.assertTrue(os.path.exists(self.path + ".quarantined"))
        with self.assertRaises(StateCorruptError):
            SendState(self.path)


class VerifySentTest(unittest.TestCase):
    OUR_TEXT = "X" * 60 + "尾巴-ours"
    SENT_AFTER = 1000.0

    @property
    def thash(self):
        return hashlib.sha256(self.OUR_TEXT.encode("utf-8")).hexdigest()

    def hist_msg(self, **kw):
        m = {"is_bot": True, "bot_id": "B_OURS", "user": "",
             "text": self.OUR_TEXT, "text_sha256": self.thash,
             "ts": "1005.0", "thread_ts": "", "client_msg_id": "cid-1"}
        m.update(kw)
        return m

    def verify(self, msgs, err=None, **kw):
        kw.setdefault("bot_id", "B_OURS")
        kw.setdefault("bot_user_id", "U_OURS")
        kw.setdefault("client_msg_id", "cid-1")
        with patch.object(pc, "channel_history",
                           return_value=(msgs, err)):
            return pc.verify_sent("C1", self.thash, "", kw["bot_id"],
                                  kw["bot_user_id"], self.SENT_AFTER,
                                  client_msg_id=kw["client_msg_id"])

    def test_other_bot_same_prefix_not_confirmed(self):
        # 复现：其他 bot 的旧消息只有 60 字前缀相同 -> 不能误确认
        other = "X" * 60 + "尾巴-theirs"
        msgs = [self.hist_msg(
            bot_id="B_OTHER",
            text_sha256=hashlib.sha256(other.encode()).hexdigest(),
            ts="1002.0")]
        self.assertFalse(self.verify(msgs))

    def test_wrong_thread_not_confirmed(self):
        msgs = [self.hist_msg(thread_ts="999.0")]
        self.assertFalse(self.verify(msgs))

    def test_message_before_attempt_not_confirmed(self):
        msgs = [self.hist_msg(ts="900.0")]  # 早于发送尝试（-60s 偏差外）
        self.assertFalse(self.verify(msgs))

    def test_exact_match_confirmed(self):
        msgs = [self.hist_msg()]
        self.assertTrue(self.verify(msgs))

    def test_match_by_user_id_when_bot_id_unknown(self):
        msgs = [self.hist_msg(bot_id="", user="U_OURS")]
        self.assertTrue(self.verify(msgs))

    def test_unknown_identity_never_confirms(self):
        msgs = [self.hist_msg()]
        self.assertFalse(self.verify(msgs, bot_id=None, bot_user_id=None))

    def test_history_error_returns_none(self):
        self.assertIsNone(self.verify([], err="boom: timeout"))

    def test_no_text_hash_cannot_confirm(self):
        with patch.object(pc, "channel_history",
                           return_value=([self.hist_msg()], None)):
            self.assertFalse(pc.verify_sent("C1", None, "", "B_OURS",
                                            "U_OURS", self.SENT_AFTER))


class TornTailTest(StoreCase):
    def test_torn_tail_quarantined_new_message_not_swallowed(self):
        self.assertTrue(inbox_store.append_record(rec("C1", "1.1")))
        # 模拟崩溃残行：半截 JSON，没有换行
        with open(inbox_store.INBOX_PATH, "a") as f:
            f.write('{"msg_id": "C1:9.9", "channel": "C1", "text": "torn')
        self.assertTrue(inbox_store.append_record(rec("C1", "2.2")))
        got = inbox_store.read_undelivered()
        # 新记录没有被粘到残行上吞掉
        self.assertEqual([m["msg_id"] for m in got], ["C1:1.1", "C1:2.2"])
        # 损坏证据保留
        ev = glob.glob(inbox_store.INBOX_PATH + ".corrupt.*")
        self.assertEqual(len(ev), 1)
        with open(ev[0], "rb") as f:
            self.assertIn(b"torn", f.read())
        # 文件以换行结尾
        self.assertTrue(self.raw().endswith(b"\n"))

    def test_duplicate_branch_confirms_durability(self):
        # 首次 append 的 fsync 失败：直接抛异常，不返回成功。
        # 记录其实已在磁盘上（write+flush 成功），但持久化未被确认。
        with patch("os.fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                inbox_store.append_record(rec("C1", "5.5"))
        # duplicate 分支：未经再次持久化确认，绝不静默返回成功。
        with patch("os.fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                inbox_store.append_record(rec("C1", "5.5"))
        # fsync 恢复后：duplicate 确认持久化后返回 False
        self.assertFalse(inbox_store.append_record(rec("C1", "5.5")))


class CompactRedeliveryTest(StoreCase):
    def test_redelivery_within_retention_not_requeued(self):
        self.assertTrue(inbox_store.append_record(rec("C1", "1.1")))
        inbox_store.ack(["C1:1.1"])
        stats = inbox_store.compact()
        self.assertEqual(stats["tombstones_kept"], 1)
        # 同一事件重投：tombstone 还在，不再入队
        self.assertFalse(inbox_store.append_record(rec("C1", "1.1")))
        self.assertEqual(inbox_store.read_undelivered(), [])

    def test_expired_tombstone_allows_requeue_documented(self):
        self.assertTrue(inbox_store.append_record(rec("C1", "1.1")))
        old = time.time() - 8 * 24 * 3600  # 8 天前：已过期
        with open(inbox_store.INBOX_PATH, "a") as f:
            f.write(json.dumps({"type": "ack", "msg_id": "C1:1.1",
                                "at": old}) + "\n")
        stats = inbox_store.compact()
        self.assertEqual(stats["tombstones_expired"], 1)
        self.assertEqual(stats["tombstones_kept"], 0)
        # 恢复规则(2)：过期后重投视为新消息（显式取舍，见文档）
        self.assertTrue(inbox_store.append_record(rec("C1", "1.1")))

    def test_compact_converts_legacy_delivered(self):
        self.assertTrue(inbox_store.append_record(rec("C1", "1.1")))
        # 绕过 API 直接写入 legacy 行（migration 缓存已建立，compact 自己处理）
        with open(inbox_store.INBOX_PATH, "a") as f:
            f.write(json.dumps(
                {"channel": "C1", "user": "U1", "text": "old",
                 "kind": "mention", "ts": "7.7", "thread_ts": "",
                 "received_at": 0.0, "delivered": True}) + "\n")
        inbox_store.compact()
        got = [m["msg_id"] for m in inbox_store.read_undelivered()]
        # legacy delivered=true 的 7.7 不可见；未确认的 1.1 仍在
        self.assertEqual(got, ["C1:1.1"])
        # 去重信息被保留为 tombstone：重投不再入队
        self.assertFalse(inbox_store.append_record(rec("C1", "7.7")))


if __name__ == "__main__":
    unittest.main()
