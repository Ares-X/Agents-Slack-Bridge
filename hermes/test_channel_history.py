#!/usr/bin/env python3
"""channel_history.py 回归测试（stdlib-only，无网络、无真实 token、无真实 ~/.hermes）。

进程内 runpy 加载 channel_history.py 并拦截其 urllib.request.urlopen：既测主读取，
也测 --resolve 的名称解析。覆盖：

  T1  --resolve 名称解析失败不得中止主要读取（d9d238d 回归）：users.info 返回
        user_not_found 时保留原 ID 继续输出所有消息；B 前缀 ID 跰由 bots.info。
  T2  响应读取边界：ConnectionResetError / http.client.IncompleteRead /
        BadStatusLine 在 d9d238d 是未捕获异常、stdout 为空；现须输出结构化
        {"error": {kind, detail}} 并退出 1，且不自动重试。
  T3  d9d238d 既有行为回归：两种模式统一时间正序、limit 前后位置随意、
        429 Retry-After、ok:false、missing token、invalid JSON。

运行: python3 test_channel_history.py   （在 hermes/ 目录下）
"""
import email.message
import http.client
import io
import json
import socket
import runpy
import sys
import urllib.error
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "channel_history.py"
CHANNEL = "C0TESTCHANNEL"
TOKEN = "xoxb-test-token-000"

# d9d238d 的复现 harness：一页历史 = 1 条正常人类消息 + 1 条只有 bot_id 的 bot 帖。
HISTORY_PAGE = [
    {"ts": "1000.0002", "user": "U0HUMAN", "text": "human msg"},
    {"ts": "1000.0001", "bot_id": "B0BOTONLY", "text": "bot msg, no user field"},
]


class SystemExitTrap(Exception):
    def __init__(self, code):
        self.code = code


def run(argv, urlopen_side_effect, env_token=TOKEN):
    """进程内跑一次 channel_history（sys.argv/urlopen/exit 全拦下），返回 (rc, stdout)。
    rc=2 表示被测脚本以未捕获异常崩溃（d9d238d 的 T2 行为）。"""
    rc_holder = {"rc": 0, "crash": ""}

    def fake_exit(code=0):
        rc_holder["rc"] = code if isinstance(code, int) else 1
        raise SystemExitTrap(code)

    stdout_buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["channel_history.py"] + list(argv)
    env = {"SLACK_BOT_TOKEN": env_token, "HOME": "/nonexistent", "HERMES_ENV": ""}
    with mock.patch.dict("os.environ", env, clear=True), \
            mock.patch("urllib.request.urlopen", side_effect=urlopen_side_effect), \
            redirect_stdout(stdout_buf), \
            mock.patch.object(sys, "exit", fake_exit):
        try:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        except SystemExitTrap:
            pass
        except BaseException as e:  # 未捕获异常 = 被测代码崩了：记 rc=2，别让测试进程死
            rc_holder["rc"] = 2
            rc_holder["crash"] = f"{type(e).__name__}: {e}"
    sys.argv = old_argv
    return rc_holder["rc"], stdout_buf.getvalue()


def ok_resp(payload):
    class FakeResp(io.BytesIO):
        def __init__(self, body):
            super().__init__(body.encode())
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    return FakeResp(json.dumps(payload))


class Router:
    """按 method 路由 fake urlopen：history/replies 页 + users.info/bots.info 应答。"""

    def __init__(self, page=None, users=None, bots=None):
        self.page = page if page is not None else list(HISTORY_PAGE)
        self.users = users or {}       # uid -> payload dict
        self.bots = bots or {}         # bid -> payload dict
        self.calls = []

    def __call__(self, req, timeout=30):
        url = req.full_url
        method = url.split("slack.com/api/")[-1].split("?")[0]
        self.calls.append(method)
        if method in ("conversations.history", "conversations.replies"):
            return ok_resp({"ok": True, "messages": self.page})
        if method == "users.info":
            uid = url.split("user=")[-1].split("&")[0]
            return ok_resp(self.users.get(uid, {"ok": False, "error": "user_not_found"}))
        if method == "bots.info":
            bid = url.split("bot=")[-1].split("&")[0]
            return ok_resp(self.bots.get(bid, {"ok": False, "error": "bot_not_found"}))
        raise AssertionError(f"unexpected method {method}")


def main():
    passed, failed = 0, []

    def check(name, cond, extra=None):
        nonlocal passed
        if cond:
            passed += 1
            print(f"  ok   {name}")
        else:
            failed.append(name)
            print(f"  FAIL {name}  {extra!r}")

    # ---------- T1: --resolve 降级 ----------
    print("T1 resolve-degrades-on-name-failure")
    # 1a. 复现 np 的场景：users.info(B…) -> user_not_found，主读取必须保留全部消息
    r = Router(users={"U0HUMAN": {"ok": True, "user": {"name": "Hugh Mann"}}})
    rc, out = run([CHANNEL, "20", "--resolve"], r)
    lines = [l for l in out.splitlines() if l.strip()]
    check("exit 0", rc == 0, f"rc={rc}")
    check("both messages printed", len(lines) == 2, lines)
    if len(lines) == 2:
        rows = [json.loads(l) for l in lines]
        # history 模式反转后：旧→新 = bot 帖在前
        check("bot-only msg kept with raw ID", rows[0]["user"] == "B0BOTONLY", rows[0])
        check("name fallback = raw bot id", rows[0].get("user_name") == "B0BOTONLY", rows[0])
        check("human name resolved", rows[1].get("user_name") == "Hugh Mann", rows[1])
    # 1b. 前缀路由：B… 走 bots.info，U… 走 users.info
    r = Router(
        users={"U0HUMAN": {"ok": True, "user": {"name": "Hugh Mann"}}},
        bots={"B0BOTONLY": {"ok": True, "bot": {"name": "Bee Bot"}}},
    )
    rc, out = run([CHANNEL, "20", "--resolve"], r)
    rows = [json.loads(l) for l in out.splitlines() if l.strip()]
    check("prefix routing exit 0", rc == 0, f"rc={rc}")
    check("B-id -> bots.info", "bots.info" in r.calls, r.calls)
    check("U-id -> users.info", "users.info" in r.calls, r.calls)
    check("bots.info name used", any(x.get("user_name") == "Bee Bot" for x in rows), rows)
    check("users.info name used", any(x.get("user_name") == "Hugh Mann" for x in rows), rows)
    # 1c. 名称解析自身网络失败（URLError(timeout)）也只降级不中止
    class RouterTimeoutNm(Router):
        def __call__(self, req, timeout=30):
            url = req.full_url
            if "users.info" in url or "bots.info" in url:
                raise urllib.error.URLError(socket.timeout())
            return super().__call__(req, timeout)

    rc, out = run([CHANNEL, "20", "--resolve"], RouterTimeoutNm())
    lines = [l for l in out.splitlines() if l.strip()]
    check("name-resolve network fail: degrade, exit 0", rc == 0 and len(lines) == 2, (rc, lines))
    if len(lines) == 2:
        rows = [json.loads(l) for l in lines]
        check("names fall back to raw ids",
              rows[0].get("user") == "B0BOTONLY" and rows[1].get("user_name") == "U0HUMAN", rows)

    # ---------- T2: 读取边界异常 ----------
    print("T2 read-boundary-exceptions")

    class ResetResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            raise ConnectionResetError("connection reset by peer")

    rc, out = run([CHANNEL, "20"], lambda req, timeout=30: ResetResp())
    lines = [l for l in out.splitlines() if l.strip()]
    check("reset: exit 1, one error line", rc == 1 and len(lines) == 1, (rc, lines))
    if lines:
        e = json.loads(lines[0])
        check("reset: kind=connection_reset", e["error"]["kind"] == "connection_reset", e)
        check("reset: detail names method", "conversations.history" in e["error"]["detail"], e)

    class IncompleteResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            raise http.client.IncompleteRead(b'{"ok":tr', 42)

    rc, out = run([CHANNEL, "20"], lambda req, timeout=30: IncompleteResp())
    lines = [l for l in out.splitlines() if l.strip()]
    check("incomplete: exit 1, one error line", rc == 1 and len(lines) == 1, (rc, lines))
    if lines:
        e = json.loads(lines[0])
        check("incomplete: kind=incomplete_read", e["error"]["kind"] == "incomplete_read", e)

    class BadStatusResp(IncompleteResp):
        def read(self, n=-1):
            raise http.client.BadStatusLine("garbage")

    rc, out = run([CHANNEL, "20"], lambda req, timeout=30: BadStatusResp())
    lines = [l for l in out.splitlines() if l.strip()]
    check("badstatus: exit 1, one error line", rc == 1 and len(lines) == 1, (rc, lines))
    if lines:
        e = json.loads(lines[0])
        check("badstatus: kind=http_protocol_error", e["error"]["kind"] == "http_protocol_error", e)

    attempts = []

    def urlopen_count(req, timeout=30):
        attempts.append(1)
        raise urllib.error.URLError(ConnectionResetError("reset"))

    rc, out = run([CHANNEL, "20"], urlopen_count)
    check("no auto-retry: exactly 1 request", len(attempts) == 1, attempts)
    lines = [l for l in out.splitlines() if l.strip()]
    if lines:
        e = json.loads(lines[0])
        check("reset-in-urlerror: kind=connection_reset", e["error"]["kind"] == "connection_reset", e)

    # ---------- T3: 既有行为回归 ----------
    print("T3 regression-of-d9d238d-behavior")
    # 3a. 频道模式正序（Slack 按最新在前返回 → 输出反转成旧→新）
    page = [
        {"ts": "1000.0003", "user": "U0HUMAN", "text": "newest"},
        {"ts": "1000.0002", "user": "U0HUMAN", "text": "middle"},
        {"ts": "1000.0001", "user": "U0HUMAN", "text": "oldest"},
    ]
    rc, out = run([CHANNEL, "20"], Router(page=page))
    rows = [json.loads(l) for l in out.splitlines() if l.strip()]
    check("history mode: oldest->newest",
          rc == 0 and [x["ts"] for x in rows] == ["1000.0001", "1000.0002", "1000.0003"], rows)
    # 3b. thread 模式原序（replies 本身旧→新，不反转）
    page = [
        {"ts": "1000.0000", "user": "U0HUMAN", "text": "parent"},
        {"ts": "1000.0001", "user": "U0HUMAN", "text": "reply"},
    ]
    rc, out = run([CHANNEL, "--thread", "1000.0000"], Router(page=page))
    rows = [json.loads(l) for l in out.splitlines() if l.strip()]
    check("thread mode: as-is order", rc == 0 and [x["text"] for x in rows] == ["parent", "reply"], rows)
    # 3c. limit 前置 / 后置
    one = [{"ts": "1000.0001", "user": "U0HUMAN", "text": "r"}]
    rc1, out1 = run([CHANNEL, "10", "--thread", "1000.0000"], Router(page=one))
    rc2, out2 = run([CHANNEL, "--thread", "1000.0000", "10"], Router(page=one))
    check("limit before --thread ok", rc1 == 0 and len(out1.splitlines()) == 1, (rc1, out1))
    check("limit after --thread ok", rc2 == 0 and len(out2.splitlines()) == 1, (rc2, out2))
    # 3d. HTTP 429 + Retry-After
    err429 = urllib.error.HTTPError(
        "https://slack.com/api/x", 429, "Too Many Requests",
        email.message.Message(), io.BytesIO(b""))
    err429.headers["Retry-After"] = "30"

    def throw429(req, timeout=30):
        raise err429

    rc, out = run([CHANNEL, "20"], throw429)
    lines = [l for l in out.splitlines() if l.strip()]
    e = json.loads(lines[0]) if lines else {"error": {}}
    check("429: kind + retry_after=30",
          rc == 1 and e["error"].get("kind") == "http_429" and e["error"].get("retry_after") == 30, (rc, e))
    # 3e. ok:false（含 429 响应体）→ slack_api_error
    rc, out = run([CHANNEL, "20"], lambda req, timeout=30: ok_resp({"ok": False, "error": "ratelimited"}))
    lines = [l for l in out.splitlines() if l.strip()]
    e = json.loads(lines[0]) if lines else {"error": {}}
    check("ok:false -> slack_api_error",
          rc == 1 and e["error"].get("kind") == "slack_api_error" and "ratelimited" in e["error"]["detail"], (rc, e))
    # 3f. missing token
    rc, out = run([CHANNEL], lambda req, timeout=30: ok_resp({"ok": True}), env_token="")
    lines = [l for l in out.splitlines() if l.strip()]
    e = json.loads(lines[0]) if lines else {"error": {}}
    check("missing token", rc == 1 and e["error"]["kind"] == "missing_token", (rc, e))
    # 3g. invalid JSON
    class HtmlResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"<html>oops"

    rc, out = run([CHANNEL, "20"], lambda req, timeout=30: HtmlResp())
    lines = [l for l in out.splitlines() if l.strip()]
    e = json.loads(lines[0]) if lines else {"error": {}}
    check("invalid json", rc == 1 and e["error"]["kind"] == "invalid_json", (rc, e))

    # T4 bounded continuation: the API gets an exclusive time bound; pagination
    # evidence is opt-in so existing JSONL consumers keep their message-only shape.
    params_seen = []

    def older_page(req, timeout=30):
        params_seen.append(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query))
        return ok_resp({"ok": True, "messages": HISTORY_PAGE, "has_more": True,
                        "response_metadata": {"next_cursor": "next-test-page"}})

    rc, out = run([CHANNEL, "100", "--before-ts", "1001.0000", "--page-info"], older_page)
    rows = [json.loads(line) for line in out.splitlines() if line]
    check("before-ts reads older page", rc == 0 and params_seen[0].get("latest") == ["1001.0000"], (rc, params_seen))
    check("before-ts excludes boundary message", params_seen[0].get("inclusive") == ["false"], params_seen)
    check("bounded page messages remain chronological", rows[0]["ts"] == "1000.0001" and rows[1]["ts"] == "1000.0002", rows)
    check("page metadata exposes truncation", rows[-1]["page_info"]["has_more"] and rows[-1]["page_info"]["next_cursor"] == "next-test-page", rows)
    check("page metadata exposes next older boundary", rows[-1]["page_info"]["oldest_ts"] == "1000.0001", rows)
    params_seen.clear()
    rc, out = run([CHANNEL, "--thread", "1000.0001", "--before-ts", "1001.0000"], older_page)
    check("thread/before conflict fails before network", rc == 1 and not params_seen and json.loads(out)["error"]["kind"] == "invalid_arguments", (rc, out))

    # ---------- 汇总 ----------
    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        print("FAILED:", *failed, sep="\n  - ")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
