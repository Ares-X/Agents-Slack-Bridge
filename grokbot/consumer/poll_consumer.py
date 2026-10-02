"""Grok Bot 消费层：~5s 轮询，拉频道历史，上下文感知模板回复，ack。

多 agent 协作默认（可用 .env 覆盖）：
  - SLACK_BRIDGE_POLL_SEC=5
  - REPLY_IN_THREAD=0 → 频道顶层回复（不传 --thread-ts；同伴看得见）
  - REPLY_IN_THREAD=1 → 跟帖：thread_ts 优先，否则用消息自身 ts（顶层开帖）
  - 每条消息回复前先 channel_history（上下文）；失败时可见降级，不假装读过
  - 本 bot user ID：SLACK_BOT_USER_ID 或 auth_test；勿 @ 自己、防回环

发送 / ack 状态机（避免 ack 失败导致双发）：
  - reply_status 空 → 尝试 send；成功则 durable 标 reply_status=sent 再 ack
  - reply_status=sent 且未 delivered → 只重试 ack，不再 send
  - reply_status=uncertain → 不盲发、不 ack；需人工/上层处理
  - inbox_ack 用 (channel, ts)，与去重一致

会话历史落在 consumer/channel_sessions.json。
脚本在 consumer/ 下，inbox_* / send / channel_history 在上一层 grokbot/，
因此 ROOT = dirname(BASE)，所有子进程 cwd=ROOT。

★ generate_reply() 是模板 stub，不是已接线的 Grok 模型。
  「模板回过一次」≠ Agent / LLM 集成完成；换成真实 LLM 后仍须使用 history。
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

# Allow `from inbox_store import ...` when run as script from consumer/
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from inbox_store import ack_keys, msg_key, set_reply_status  # noqa: E402


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
    """Bot user ID from env, else auth_test. Never hardcode production IDs."""
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
    """回复前拉历史做上下文。

    Returns (messages, error_or_None).
    Preserves real errors from subprocess / error JSON; never pretends success.
    """
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
        # Partial: keep messages but surface the error too.
        return out, err
    return out, None


def strip_mentions(text):
    return re.sub(r"<@[^>]+>", "", text or "").strip()


def thread_target(message):
    """For REPLY_IN_THREAD=1: existing thread_ts else message ts (start thread)."""
    return (message.get("thread_ts") or message.get("ts") or "").strip()


def generate_reply(channel, message, session, history, history_error=None):
    """短上下文感知模板回复（stub，非 Grok 模型接线）。

    ★ replace with LLM API or wake your Grok Bot agent.
    不要硬编码真实 bot user ID；需要 @ 其他 agent 时用占位符，例如 <@U_PEER_BOT_ID>。
    若 history_error：不得声称「已读上下文」。
    """
    text = strip_mentions(message.get("text") or "")
    user = message.get("user_name") or message.get("user") or "someone"

    degrade = ""
    if history_error:
        degrade = f"（注意：频道历史读取失败，本次无上下文：{history_error[:120]}）"

    # 最近非自己的历史作上下文提示
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


def _ack_message(m):
    """Ack by (channel, ts). Returns True on success (exit 0)."""
    ch, ts = msg_key(m)
    if not ch or not ts:
        print(f"ack skipped: missing channel/ts in {m!r}", file=sys.stderr)
        return False
    n, missing = ack_keys(INBOX_PATH, {(ch, ts)})
    if n > 0 and not missing:
        return True
    # Also try CLI for visibility in logs when library path differs
    r = sh(sys.executable, "inbox_ack.py", ch, ts)
    ok = r.returncode == 0
    if not ok:
        print(
            f"ack failed for {ch}:{ts} rc={r.returncode} "
            f"stdout={r.stdout!r} stderr={r.stderr!r}",
            file=sys.stderr,
        )
    return ok


def _mark_reply_status(m, status):
    ch, ts = msg_key(m)
    n, missing = set_reply_status(INBOX_PATH, {(ch, ts)}, status)
    if n == 0 or missing:
        print(
            f"mark reply_status={status} failed for {ch}:{ts} "
            f"(matched={n}, missing={missing})",
            file=sys.stderr,
        )
        return False
    return True


def classify_send_result(proc):
    """Return 'ok' | 'fail' | 'uncertain' from send.py subprocess result."""
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and "sent ok: True" in (proc.stdout or ""):
        return "ok"
    if proc.returncode != 0 and "sent ok: True" not in out:
        return "fail"
    # Nonzero with success text, or zero without clear success → uncertain
    if "sent ok: True" in out and proc.returncode != 0:
        return "uncertain"
    if proc.returncode == 0 and "sent ok:" in (proc.stdout or ""):
        # e.g. sent ok: False
        if "sent ok: True" not in (proc.stdout or ""):
            return "fail"
    return "uncertain"


def handle_one(m, sessions):
    """Process one undelivered message. Returns True if newly replied this round."""
    if ME and m.get("user") == ME:
        _ack_message(m)
        return False

    status = m.get("reply_status")
    ch = m.get("channel") or ""
    ts = m.get("ts") or ""
    label = m.get("channel_name") or ch

    # Already sent: only retry ack (do not resend).
    if status == "sent":
        if _ack_message(m):
            print(f"acked prior send in {label} ({ch}:{ts})", flush=True)
        return False

    # Uncertain prior send: do not blindly resend.
    if status == "uncertain":
        print(
            f"skip uncertain send outcome for {ch}:{ts} "
            f"(manual check; will not resend)",
            file=sys.stderr,
        )
        return False

    sess = sessions.setdefault(ch, [])
    hist, hist_err = channel_history(ch)
    if hist_err:
        print(f"history degrade for {ch}: {hist_err}", file=sys.stderr)

    try:
        reply = generate_reply(ch, m, sess, hist, history_error=hist_err)
        cmd = [sys.executable, "send.py", ch]
        if REPLY_IN_THREAD:
            tt = thread_target(m)
            if tt:
                cmd += ["--thread-ts", tt]
        r = sh(*cmd, input_text=reply)
        outcome = classify_send_result(r)
        if outcome == "ok":
            if not _mark_reply_status(m, "sent"):
                # Send succeeded but could not persist status → treat as uncertain
                # to avoid double-send on next loop if ack also fails.
                print(
                    f"send ok but status mark failed for {ch}:{ts}; "
                    f"will not blind-resend",
                    file=sys.stderr,
                )
                return False
            sess.append({"role": "user", "text": m.get("text")})
            sess.append({"role": "assistant", "text": reply})
            sessions[ch] = sess[-40:]
            if _ack_message(m):
                print(f"replied in {label}", flush=True)
            else:
                print(
                    f"replied in {label} but ack failed "
                    f"(will retry ack only next round)",
                    flush=True,
                )
            return True
        if outcome == "fail":
            print(
                f"send failed for {ch}:{ts}: {r.stdout} {r.stderr}",
                file=sys.stderr,
            )
            return False
        # uncertain
        _mark_reply_status(m, "uncertain")
        print(
            f"send outcome uncertain for {ch}:{ts}: "
            f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}; "
            f"will NOT resend",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        # Exception after possible partial send → mark uncertain, do not resend.
        _mark_reply_status(m, "uncertain")
        print(f"error handling {ch}:{ts}: {e}", file=sys.stderr)
        return False


def main():
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
