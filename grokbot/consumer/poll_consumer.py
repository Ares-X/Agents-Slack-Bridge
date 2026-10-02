"""Grok Bot 消费层：~5s 轮询，拉频道历史，上下文感知模板回复，ack。

多 agent 协作默认（可用 .env 覆盖）：
  - SLACK_BRIDGE_POLL_SEC=5
  - REPLY_IN_THREAD=0 → 频道顶层回复
  - REPLY_IN_THREAD=1 → 跟帖：thread_ts 优先，否则消息 ts
  - 每条消息回复前先 channel_history；失败可见降级
  - 本 bot user ID：SLACK_BOT_USER_ID 或 auth_test

发送状态机（reply_pipeline，与 pending fallback 共用）：
  - 先 durable claim (sending)，失败则不 send
  - 仅当输出证明未发送 (not_sent: / sent ok: False) 才 retryable
  - 其余失败 → uncertain；sending/uncertain 永不盲发
  - sent → 只重试 ack
  - 启动时 escalate stale sending → uncertain

★ generate_reply() 是模板 stub，不是已接线的 Grok 模型。
"""
import json
import os
import re
import ssl
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)  # grokbot/
SESSIONS_PATH = os.path.join(BASE, "channel_sessions.json")
ENV_PATH = os.path.join(ROOT, ".env")
INBOX_PATH = os.path.join(ROOT, "inbox.jsonl")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from inbox_store import (  # noqa: E402
    ack_keys,
    escalate_stale_sending,
    msg_key,
)
from reply_pipeline import (  # noqa: E402
    classify_send_result,
    process_one,
    run_send,
)


def load_env(path):
    d = {}
    if not os.path.exists(path):
        return d
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


_ENV = load_env(ENV_PATH)
POLL_INTERVAL = float(
    os.environ.get("SLACK_BRIDGE_POLL_SEC")
    or _ENV.get("SLACK_BRIDGE_POLL_SEC")
    or "5"
)
_raw_thread = (
    os.environ.get("REPLY_IN_THREAD")
    or _ENV.get("REPLY_IN_THREAD")
    or "0"
).strip().lower()
REPLY_IN_THREAD = _raw_thread in ("1", "true", "yes", "on")


def resolve_me():
    me = (_ENV.get("SLACK_BOT_USER_ID") or os.environ.get("SLACK_BOT_USER_ID") or "").strip()
    if me:
        return me
    token = _ENV.get("SLACK_BOT_TOKEN")
    if not token:
        return ""
    try:
        from slack_sdk.web import WebClient
        proxy = _ENV.get("PROXY_URL") or None
        ca = _ENV.get("CA_BUNDLE") or None
        ctx = ssl.create_default_context(
            cafile=ca if ca and os.path.exists(ca) else None)
        kw = {"token": token, "ssl": ctx}
        if proxy:
            kw["proxy"] = proxy
        return WebClient(**kw).auth_test().get("user_id") or ""
    except Exception as e:
        print(f"auth_test failed: {e}", file=sys.stderr)
        return ""


ME = resolve_me()


def sh(*args, input_text=None):
    return subprocess.run(args, input=input_text, capture_output=True,
                          text=True, cwd=ROOT)


def peek():
    r = sh(sys.executable, "inbox_peek.py")
    out = []
    for line in r.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load_sessions():
    if os.path.exists(SESSIONS_PATH):
        try:
            with open(SESSIONS_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sessions(s):
    tmp = SESSIONS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_PATH)


def channel_history(channel, limit=15):
    r = sh(sys.executable, "channel_history.py", channel, str(limit))
    out = []
    err = None
    if r.returncode != 0:
        err = (
            f"history subprocess exit {r.returncode}: "
            f"{(r.stderr or r.stdout or '').strip()[:300]}"
        )
    for line in (r.stdout or "").splitlines():
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "error" in m:
            err = str(m.get("error") or "history error")
            continue
        out.append(m)
    if err and not out:
        return [], err
    if err and out:
        return out, err
    return out, None


def strip_mentions(text):
    return re.sub(r"<@[^>]+>", "", text or "").strip()


def thread_target(message):
    return (message.get("thread_ts") or message.get("ts") or "").strip()


def generate_reply(channel, message, session, history, history_error=None):
    """短上下文感知模板回复（stub，非 Grok 模型接线）。"""
    text = strip_mentions(message.get("text") or "")
    user = message.get("user_name") or message.get("user") or "someone"

    degrade = ""
    if history_error:
        degrade = f"（注意：频道历史读取失败，本次无上下文：{history_error[:120]}）"

    ctx = []
    for h in history[-10:]:
        uname = h.get("user_name") or h.get("user") or ""
        if ME and (h.get("user") == ME or uname == ME):
            continue
        t = strip_mentions(h.get("text") or "")
        if not t:
            continue
        ctx.append(t[:160])

    low = text.lower()
    if "上下文" in text or "聊天记录" in text or "互相交流" in text:
        if history_error:
            return (
                "收到。我本应拉本频道最近消息再回，但这次历史读取失败，"
                f"没有可用上下文。{degrade}"
            )
        return (
            "收到。我会在被 @ 时先拉本频道最近消息再回；"
            "只在被点名时发言，不 @ 自己，避免回环。"
        )
    if "认识" in text or "你是谁" in text or "who are you" in low:
        base = "我是 Grok Bot。被 @ 就会回；能拉频道历史当上下文。"
        return base + (degrade and (" " + degrade) or "")
    if not text:
        return f"在，{user}。" + (degrade and (" " + degrade) or "")

    hint = ctx[-1] if ctx else ""
    if history_error:
        return (
            f"收到「{text[:120]}」。{degrade} "
            "说下你要我具体做什么。"
        )
    if hint and hint != text:
        return (
            f"看到了频道上下文。关于「{text[:120]}」："
            f"说下你要我具体做什么。"
        )
    return f"收到。你要我针对「{text[:120]}」做什么？"


def handle_one(m, sessions, *, runner=None):
    """Process one undelivered message via shared reply_pipeline."""
    if ME and m.get("user") == ME:
        ack_keys(INBOX_PATH, {msg_key(m)})
        return False

    ch = m.get("channel") or ""
    ts = m.get("ts") or ""
    label = m.get("channel_name") or ch
    status = m.get("reply_status")

    # Fast paths that need no reply text
    if status == "sent":
        result = process_one(
            INBOX_PATH, m, "", reply_in_thread=REPLY_IN_THREAD,
            root=ROOT, runner=runner,
        )
        if result.get("outcome") == "acked":
            print(f"acked prior send in {label} ({ch}:{ts})", flush=True)
        return False

    if status in ("uncertain", "sending"):
        print(
            f"skip {status} for {ch}:{ts} (will not resend; verify manually)",
            file=sys.stderr,
        )
        return False

    if status == "rate_limited":
        import time as _time
        try:
            until = float(m.get("retry_after_until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        now = _time.time()
        if now < until:
            print(
                f"rate-limited wait for {ch}:{ts}: "
                f"{until - now:.1f}s remaining; not sending",
                file=sys.stderr,
            )
            return False
        # Wait expired → fall through to normal history + claim + send.

    sess = sessions.setdefault(ch, [])
    hist, hist_err = channel_history(ch)
    if hist_err:
        print(f"history degrade for {ch}: {hist_err}", file=sys.stderr)

    reply = generate_reply(ch, m, sess, hist, history_error=hist_err)
    tt = thread_target(m) if REPLY_IN_THREAD else None

    def _runner(*args, input_text=None):
        if runner is not None:
            return runner(*args, input_text=input_text)
        return sh(*args, input_text=input_text)

    result = process_one(
        INBOX_PATH,
        m,
        reply,
        reply_in_thread=REPLY_IN_THREAD,
        thread_ts=tt,
        root=ROOT,
        runner=_runner,
    )
    outcome = result.get("outcome")
    if outcome in ("sent_acked", "sent_ack_pending"):
        sess.append({"role": "user", "text": m.get("text")})
        sess.append({"role": "assistant", "text": reply})
        sessions[ch] = sess[-40:]
        if outcome == "sent_acked":
            print(f"replied in {label}", flush=True)
        else:
            print(
                f"replied in {label} but ack pending "
                f"(will retry ack only next round)",
                flush=True,
            )
        return True
    if outcome == "wait_rate_limit":
        print(
            f"rate-limited wait for {ch}:{ts}: "
            f"{result.get('wait_sec', 0):.1f}s remaining "
            f"(until {result.get('retry_after_until')}); not sending",
            file=sys.stderr,
        )
        return False
    if outcome == "rate_limited":
        print(
            f"rate-limited for {ch}:{ts}: retry after "
            f"{result.get('retry_after_sec')}s "
            f"(until {result.get('retry_after_until')})",
            file=sys.stderr,
        )
        return False
    if outcome == "fail_retryable":
        print(
            f"send proven-not-sent for {ch}:{ts}: "
            f"{result.get('stderr') or result.get('stdout')}",
            file=sys.stderr,
        )
        return False
    if outcome == "uncertain":
        print(
            f"send outcome uncertain for {ch}:{ts}: {result}; will NOT resend",
            file=sys.stderr,
        )
        return False
    if outcome == "claim_persist_failed":
        print(
            f"claim persist failed for {ch}:{ts}: {result.get('detail')}; "
            f"did NOT send",
            file=sys.stderr,
        )
        return False
    if outcome == "claim_lost":
        print(f"claim lost for {ch}:{ts} (another worker?)", file=sys.stderr)
        return False
    return False


def main():
    n = escalate_stale_sending(INBOX_PATH)
    if n:
        print(f"escalated {n} stale sending → uncertain", flush=True)
    print(
        f"grokbot consumer polling every {POLL_INTERVAL}s "
        f"(REPLY_IN_THREAD={REPLY_IN_THREAD}, me={ME or 'auto-pending'}, ROOT={ROOT})",
        flush=True,
    )
    print(
        "NOTE: generate_reply() is a template stub — not auto-wired to Grok models.",
        flush=True,
    )
    while True:
        try:
            msgs = peek()
            if msgs:
                sessions = load_sessions()
                for m in msgs:
                    handle_one(m, sessions)
                save_sessions(sessions)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
