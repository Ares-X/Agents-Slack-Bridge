"""Behavioral tests for the second reliability review round (PR #5 follow-ups).

Run:  python -m unittest discover -s tests -v   (from muse/)

Covers:
  P1-1  stale .bak restore fails closed; strict version/structure
        validation; per-source quarantine evidence; ack raising -> unacked
  P1-2  durable inbox tombstone participates in the claim decision
        (stale-snapshot consumer cannot resend)
  P1-3  verify_sent requires per-attempt client_msg_id evidence
  P2-4  migration recovery / first create / duplicate append confirm
        directory durability; persistent dir-sync failure keeps refusing
  P2-5  HTTP 429 honored with Retry-After and bounded in-process retry;
        other errors stay uncertain; SDK auto-retry stays off
"""
import glob
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "consumer"))

import inbox_store
import poll_consumer as pc
from send_state import SendState, StateCorruptError
import send as send_mod


def make_state(tmpdir, with_inbox=False):
    path = os.path.join(tmpdir, "send_state.json")
    if with_inbox:
        ibx = os.path.join(tmpdir, "inbox.jsonl")
        ibx_lock = os.path.join(tmpdir, "inbox.lock")
        return SendState(path, inbox_path=ibx, inbox_lock_path=ibx_lock), ibx
    return SendState(path), None


def write_tombstone(ibx, mid):
    with open(ibx, "a") as f:
        f.write(json.dumps({"type": "ack", "msg_id": mid, "at": 1.0}) + "\n")


class P1StaleBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1bak_")
        self.path = os.path.join(self.tmp, "send_state.json")

    def test_only_stale_bak_fails_closed(self):
        # 复现：发送成功、ack 抛异常后 primary 含 sending（见下个测试），
        # .bak 仍是领取前的版本；损坏 primary 后绝不能用 .bak 恢复。
        st = SendState(self.path)
        st.claim("C1:1", channel="C1", thread_ts="", text_hash="h")
        st.claim("C1:2", channel="C1", thread_ts="", text_hash="h2")
        # 第二次 claim 触发 .bak 轮转：.bak 缺少 C1:2 的领取记录
        with open(self.path, "w") as f:
            f.write("garbage{{{")
        with self.assertRaises(StateCorruptError):
            SendState(self.path)
        # C1:2 绝不能变成可直接发送：构造直接失败，不存在"空状态可领取"
        self.assertTrue(os.path.exists(self.path + ".quarantined"))

    def test_strict_validation_rejects_bad_status(self):
        with open(self.path, "w") as f:
            json.dump({"v": 2,
                       "sends": {"C1:1": {"status": "definitely-sent"}},
                       "hist_deferred": {}}, f)
        with self.assertRaises(StateCorruptError):
            SendState(self.path)

    def test_strict_validation_rejects_sends_not_dict(self):
        with open(self.path, "w") as f:
            json.dump({"v": 2, "sends": ["C1:1"], "hist_deferred": {}}, f)
        with self.assertRaises(StateCorruptError):
            SendState(self.path)

    def test_strict_validation_rejects_wrong_version(self):
        with open(self.path, "w") as f:
            json.dump({"v": 99, "sends": {}, "hist_deferred": {}}, f)
        with self.assertRaises(StateCorruptError):
            SendState(self.path)

    def test_tmp_recovery_still_safe(self):
        # .tmp 是崩溃写一半留下的 fsync 过的更新版本：恢复安全。
        st = SendState(self.path)
        st.claim("C1:1", channel="C1", thread_ts="", text_hash="h")
        newer = {"v": 2,
                 "sends": {"C1:9": {"status": "uncertain", "attempts": 1,
                                    "updated_at": 1.0}},
                 "hist_deferred": {}}
        with open(self.path + ".tmp", "w") as f:
            json.dump(newer, f)
        with open(self.path, "w") as f:
            f.write("garbage{{{")
        st2 = SendState(self.path)  # 不抛异常
        e = st2.get("C1:9")
        self.assertIsNotNone(e)
        self.assertEqual(e["status"], "uncertain")

    def test_quarantine_keeps_each_source(self):
        st = SendState(self.path)
        st.claim("C1:1", channel="C1", thread_ts="", text_hash="h")
        for p in (self.path, self.path + ".tmp", self.path + ".bak"):
            with open(p, "w") as f:
                f.write("garbage{{{")
        with self.assertRaises(StateCorruptError):
            SendState(self.path)
        corrupt = glob.glob(self.path + ".corrupt.*")
        tags = {p.rsplit(".", 1)[-1] for p in corrupt}
        self.assertEqual(tags, {"primary", "tmp", "bak"})

    def test_ack_exception_becomes_unacked_not_sending(self):
        # P1-1 复现前半：发送成功但 ack 抛异常 -> unacked（只重试 ack），
        # 绝不能卡在 sending。
        st = SendState(self.path)
        m = {"msg_id": "C1:5", "channel": "C1", "text": "hi", "thread_ts": ""}
        with patch.object(pc, "channel_history",
                           return_value=([], None)), \
             patch.object(pc, "generate_reply", return_value="reply"), \
             patch.object(pc, "send_reply", return_value="ok"), \
             patch.object(pc, "ack", side_effect=RuntimeError("boom")):
            res = pc.handle_one(m, {}, st,
                                bot_id="B_OURS", bot_user_id="U_OURS")
        self.assertEqual(res, "sent-unacked")
        self.assertEqual(st.get("C1:5")["status"], "unacked")


class P1TombstoneClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1tomb_")

    def test_claim_completed_when_tombstone_exists(self):
        st, ibx = make_state(self.tmp, with_inbox=True)
        write_tombstone(ibx, "C1:1")
        entry, outcome = st.claim("C1:1", channel="C1", thread_ts="",
                                  text_hash="h")
        self.assertEqual(outcome, "completed")
        self.assertIsNone(entry)

    def test_claim_ok_without_tombstone(self):
        st, ibx = make_state(self.tmp, with_inbox=True)
        entry, outcome = st.claim("C1:1", channel="C1", thread_ts="",
                                  text_hash="h")
        self.assertEqual(outcome, "claimed")

    def test_stale_snapshot_cannot_reclaim_after_resolve(self):
        # 复现 P1-2：A 发送+ack+resolve；B 拿着旧快照随后 claim。
        st, ibx = make_state(self.tmp, with_inbox=True)
        e1, o1 = st.claim("C1:1", channel="C1", thread_ts="", text_hash="h")
        self.assertEqual(o1, "claimed")
        write_tombstone(ibx, "C1:1")   # A ack
        st.resolve("C1:1")              # A resolve（条目删除）
        # B 的旧快照：state.get 看不到条目，但 tombstone 参与领取判断
        entry, outcome = st.claim("C1:1", channel="C1", thread_ts="",
                                  text_hash="h")
        self.assertEqual(outcome, "completed")

    def test_handle_one_skips_completed_without_sending(self):
        st, ibx = make_state(self.tmp, with_inbox=True)
        write_tombstone(ibx, "C1:7")
        m = {"msg_id": "C1:7", "channel": "C1", "text": "hi", "thread_ts": ""}
        with patch.object(pc, "channel_history",
                           return_value=([], None)), \
             patch.object(pc, "generate_reply", return_value="reply"), \
             patch.object(pc, "send_reply") as msend:
            res = pc.handle_one(m, {}, st,
                                bot_id="B_OURS", bot_user_id="U_OURS")
        self.assertEqual(res, "already-acked")
        msend.assert_not_called()

    def test_tombstone_written_between_get_and_claim_is_seen(self):
        # get（无条目）与 claim 之间写入 tombstone：claim 必须看到。
        st, ibx = make_state(self.tmp, with_inbox=True)
        self.assertIsNone(st.get("C1:3"))
        write_tombstone(ibx, "C1:3")
        _, outcome = st.claim("C1:3", channel="C1", thread_ts="",
                              text_hash="h")
        self.assertEqual(outcome, "completed")


class P1ClientMsgIdTest(unittest.TestCase):
    TEXT = "same text both times"
    TS_OLD = 970.0     # 30 秒前的相同正文（60s 偏差内）
    TS_NOW = 1000.0

    def hist(self, client_msg_id):
        thash = hashlib.sha256(self.TEXT.encode("utf-8")).hexdigest()
        return [{"is_bot": True, "bot_id": "B_OURS", "user": "U_OURS",
                 "thread_ts": "", "ts": str(self.TS_OLD),
                 "text_sha256": thash, "client_msg_id": client_msg_id}]

    def thash(self):
        return hashlib.sha256(self.TEXT.encode("utf-8")).hexdigest()

    def test_old_duplicate_text_is_not_proof(self):
        # P1-3 复现：30 秒前发过相同正文（旧尝试的 id），本次 uncertain
        # 绝不能被误判为 verified-acked。
        with patch.object(pc, "channel_history",
                           return_value=(self.hist("old-attempt-id"), None)):
            v = pc.verify_sent("C1", self.thash(), "", "B_OURS", "U_OURS",
                               self.TS_NOW, client_msg_id="new-attempt-id")
        self.assertFalse(v)

    def test_matching_client_msg_id_proves_send(self):
        with patch.object(pc, "channel_history",
                           return_value=(self.hist("new-attempt-id"), None)):
            v = pc.verify_sent("C1", self.thash(), "", "B_OURS", "U_OURS",
                               self.TS_NOW, client_msg_id="new-attempt-id")
        self.assertTrue(v)

    def test_missing_client_msg_id_cannot_prove(self):
        # 旧版本条目没有 client_msg_id：证明不了，保持 uncertain。
        msgs = self.hist("whatever")
        with patch.object(pc, "channel_history", return_value=(msgs, None)):
            v = pc.verify_sent("C1", self.thash(), "", "B_OURS", "U_OURS",
                               self.TS_NOW, client_msg_id=None)
        self.assertFalse(v)

    def test_history_without_field_cannot_prove(self):
        # history 没回显 client_msg_id：同样证明不了。
        msgs = self.hist("")
        with patch.object(pc, "channel_history", return_value=(msgs, None)):
            v = pc.verify_sent("C1", self.thash(), "", "B_OURS", "U_OURS",
                               self.TS_NOW, client_msg_id="new-attempt-id")
        self.assertFalse(v)

    def test_claim_persists_client_msg_id(self):
        tmp = tempfile.mkdtemp(prefix="p1cid_")
        st = SendState(os.path.join(tmp, "send_state.json"))
        entry, outcome = st.claim("C1:1", channel="C1", thread_ts="",
                                  text_hash="h", client_msg_id="abc123")
        self.assertEqual(outcome, "claimed")
        self.assertEqual(st.get("C1:1")["client_msg_id"], "abc123")


class P2DirSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2dir_")
        self._old_inbox = inbox_store.INBOX_PATH
        self._old_lock = inbox_store.LOCK_PATH
        inbox_store.INBOX_PATH = os.path.join(self.tmp, "inbox.jsonl")
        inbox_store.LOCK_PATH = os.path.join(self.tmp, "inbox.lock")
        inbox_store._MIGRATED.pop(inbox_store.INBOX_PATH, None)
        self.addCleanup(self.restore)

    def restore(self):
        inbox_store.INBOX_PATH = self._old_inbox
        inbox_store.LOCK_PATH = self._old_lock
        inbox_store._MIGRATED.pop(os.path.join(self.tmp, "inbox.jsonl"), None)

    def rec(self, mid):
        return {"msg_id": mid, "channel": "C1", "user": "U1", "text": "hi",
                "kind": "mention", "ts": "1.1", "thread_ts": "",
                "received_at": 0.0}

    def test_persistent_dir_eio_keeps_refusing(self):
        # P2-4 复现：持续目录 EIO 时，每次追加都必须抛异常（拒绝 ACK），
        # 不能第一次 error 之后就 True/False 放行。
        legacy = {"channel": "C1", "user": "U1", "text": "hi",
                  "kind": "mention", "ts": "1.1", "thread_ts": "",
                  "received_at": 0.0, "delivered": False}
        with open(inbox_store.INBOX_PATH, "w") as f:
            f.write(json.dumps(legacy) + "\n")
        calls = []
        def boom():
            calls.append(1)
            raise OSError("dir EIO")
        with patch.object(inbox_store, "_fsync_dir", side_effect=boom):
            for _ in range(3):
                with self.assertRaises(OSError):
                    inbox_store.append_record(self.rec("C1:1.1"))
        # 目录同步每次都被尝试（不止一次），且从未返回成功
        self.assertGreaterEqual(len(calls), 3)

    def test_first_create_confirms_dir(self):
        calls = []
        orig = inbox_store._fsync_dir
        def rec_fsync():
            calls.append(1)
            return orig()
        with patch.object(inbox_store, "_fsync_dir", side_effect=rec_fsync):
            ok = inbox_store.append_record(self.rec("C1:1.1"))
        self.assertTrue(ok)
        self.assertGreaterEqual(len(calls), 1)

    def test_duplicate_append_confirms_dir(self):
        self.assertTrue(inbox_store.append_record(self.rec("C1:1.1")))
        calls = []
        orig = inbox_store._fsync_dir
        def rec_fsync():
            calls.append(1)
            return orig()
        with patch.object(inbox_store, "_fsync_dir", side_effect=rec_fsync):
            ok = inbox_store.append_record(self.rec("C1:1.1"))
        self.assertFalse(ok)
        self.assertGreaterEqual(len(calls), 1)

    def test_migration_recovery_confirms_dir(self):
        # 迁移 replace 成功但目录 fsync 未确认时，重试必须补上目录同步。
        legacy = {"channel": "C1", "user": "U1", "text": "hi",
                  "kind": "mention", "ts": "2.2", "thread_ts": "",
                  "received_at": 0.0, "delivered": False}
        with open(inbox_store.INBOX_PATH, "w") as f:
            f.write(json.dumps(legacy) + "\n")
        calls = []
        orig = inbox_store._fsync_dir
        state = {"fail_once": True}
        def flaky():
            calls.append(1)
            if state["fail_once"]:
                state["fail_once"] = False
                raise OSError("dir EIO once")
            return orig()
        with patch.object(inbox_store, "_fsync_dir", side_effect=flaky):
            with self.assertRaises(OSError):
                inbox_store.append_record(self.rec("C1:3.3"))
            # 重试：目录同步被补上，返回成功
            ok = inbox_store.append_record(self.rec("C1:3.3"))
        self.assertTrue(ok)
        self.assertGreaterEqual(len(calls), 2)


class FakeResponse:
    def __init__(self, status_code, error, retry_after="1"):
        self.status_code = status_code
        self._error = error
        self.headers = {"Retry-After": retry_after}

    def get(self, k, default=None):
        return {"error": self._error}.get(k, default)


class FakeSlackApiError(Exception):
    def __init__(self, status_code=429, error="ratelimited", retry_after="1"):
        super().__init__(error)
        self.response = FakeResponse(status_code, error, retry_after)


class FakeWebClient:
    instances = []

    def __init__(self, token=None, **kw):
        self.token = token
        self.kw = kw
        self.calls = []
        self.behaviors = []
        FakeWebClient.instances.append(self)

    def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)
        b = self.behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


def run_send(argv, stdin_text, behaviors):
    """Run send.main() with a fake slack_sdk.  Returns (exit_code,
    stdout, stderr, client_instance)."""
    import types
    FakeWebClient.instances.clear()
    fake_web = types.ModuleType("slack_sdk.web")
    fake_web.WebClient = FakeWebClient
    fake_sdk = types.ModuleType("slack_sdk")
    fake_sdk.web = fake_web
    real_exists = os.path.exists

    def fake_exists(p):
        # send.py 用 os.path.exists 守卫才调 load_env（load_env 已 mock）；
        # 让 .env 看起来存在，其他路径走真实判定。
        if isinstance(p, str) and p.endswith(".env"):
            return True
        return real_exists(p)

    with patch.dict(sys.modules,
                    {"slack_sdk": fake_sdk, "slack_sdk.web": fake_web}), \
         patch.object(sys, "argv", ["send.py"] + argv), \
         patch.object(sys, "stdin", io.StringIO(stdin_text)), \
         patch.object(send_mod, "load_env",
                      return_value={"SLACK_BOT_TOKEN": "xoxb-fake"}), \
         patch.object(os.path, "exists", side_effect=fake_exists), \
         patch("time.sleep") as msleep:
        orig_init = FakeWebClient.__init__

        def init(self, token=None, **kw):
            orig_init(self, token, **kw)
            self.behaviors = list(behaviors)

        with patch.object(FakeWebClient, "__init__", init):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, "stdout", out), \
                 patch.object(sys, "stderr", err):
                try:
                    send_mod.main()
                    code = 0
                except SystemExit as e:
                    code = e.code
        inst = FakeWebClient.instances[-1]
        return code, out.getvalue(), err.getvalue(), inst, msleep


class P2RateLimitTest(unittest.TestCase):
    def test_ratelimit_retries_with_retry_after_then_succeeds(self):
        code, out, err, inst, msleep = run_send(
            ["C1", "--client-msg-id", "att-1"], "hello",
            [FakeSlackApiError(429, retry_after="2"),
             {"ok": True, "ts": "123.456"}])
        self.assertEqual(code, 0)
        self.assertIn("sent ok: True", out)
        self.assertEqual(len(inst.calls), 2)
        # Retry-After 被遵守
        msleep.assert_called_once_with(2.0)
        # client_msg_id 随 POST 提交
        self.assertEqual(inst.calls[0]["client_msg_id"], "att-1")
        # SDK 自动重试保持关闭
        self.assertEqual(inst.kw.get("retry_handlers"), [])

    def test_ratelimit_zero_retry_after_retries_immediately(self):
        code, out, err, inst, msleep = run_send(
            ["C1"], "hello",
            [FakeSlackApiError(429, retry_after="0"),
             {"ok": True, "ts": "1.0"}])
        self.assertIn("sent ok: True", out)
        self.assertEqual(len(inst.calls), 2)
        msleep.assert_not_called()

    def test_ratelimit_budget_exhausted_is_not_sent(self):
        code, out, err, inst, msleep = run_send(
            ["C1"], "hello",
            [FakeSlackApiError(429, retry_after="60")] * 10)
        self.assertEqual(code, 1)
        self.assertIn("RESULT not-sent", err)
        self.assertIn("ratelimited", err)
        # 有界：不会无限重试
        self.assertLessEqual(len(inst.calls), 5)

    def test_non_ratelimit_api_error_stays_uncertain(self):
        code, out, err, inst, msleep = run_send(
            ["C1"], "hello",
            [FakeSlackApiError(404, error="channel_not_found")])
        self.assertEqual(code, 2)
        self.assertIn("RESULT uncertain", err)
        self.assertEqual(len(inst.calls), 1)

    def test_connection_error_stays_uncertain(self):
        code, out, err, inst, msleep = run_send(
            ["C1"], "hello", [ConnectionError("reset")])
        self.assertEqual(code, 2)
        self.assertIn("RESULT uncertain", err)
        self.assertEqual(len(inst.calls), 1)


if __name__ == "__main__":
    unittest.main()
