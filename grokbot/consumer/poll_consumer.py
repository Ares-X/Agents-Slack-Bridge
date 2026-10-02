"""Grok Bot 消费层：~5s 轮询，拉频道历史，上下文感知模板回复，ack。

多 agent 协作默认（可用 .env 覆盖）：
  - SLACK_BRIDGE_POLL_SEC=5
  - REPLY_IN_THREAD=0 → 频道顶层回复（不传 --thread-ts；同伴看得见）
  - 每条消息回复前先 channel_history（上下文）
  - 本 bot user ID：SLACK_BOT_USER_ID 或 auth_test；勿 @ 自己、防回环

会话历史落在 consumer/channel_sessions.json。
脚本在 consumer/ 下，inbox_peek / send / channel_history 在上一层 grokbot/，
因此 ROOT = dirname(BASE)，所有子进程 cwd=ROOT。

把 generate_reply() 换成 LLM API 或 wake your Grok Bot agent（须使用 history）。
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
    """回复前拉历史做上下文（失败返回空，不阻塞）。"""
    r = sh(sys.executable, "channel_history.py", channel, str(limit))
    out = []
    for line in r.stdout.splitlines():
        try:
            m = json.loads(line)
            if "error" not in m:
                out.append(m)
        except Exception:
            continue
    return out


def strip_mentions(text):
    return re.sub(r"<@[^>]+>", "", text or "").strip()


def generate_reply(channel, message, session, history):
    """短上下文感知模板回复。

    ★ replace with LLM API or wake your Grok Bot agent.
    不要硬编码真实 bot user ID；需要 @ 其他 agent 时用占位符，例如 <@U_PEER_BOT_ID>。
    """
    text = strip_mentions(message.get("text") or "")
    user = message.get("user_name") or message.get("user") or "someone"

    # 最近非自己的历史作上下文提示
    ctx = []
    for h in history[-10:]:
        # channel_history 行有 user_name / text / is_bot；兼容 user 字段
        uname = h.get("user_name") or h.get("user") or ""
        if ME and (h.get("user") == ME or uname == ME):
            continue
        t = strip_mentions(h.get("text") or "")
        if not t:
            continue
        ctx.append(t[:160])

    low = text.lower()
    if "上下文" in text or "聊天记录" in text or "互相交流" in text:
        return (
            "收到。我会在被 @ 时先拉本频道最近消息再回；"
            "只在被点名时发言，不 @ 自己，避免回环。"
        )
    if "认识" in text or "你是谁" in text or "who are you" in low:
        return "我是 Grok Bot。被 @ 就会回；能拉频道历史当上下文。"
    if not text:
        return f"在，{user}。"

    hint = ctx[-1] if ctx else ""
    if hint and hint != text:
        return (
            f"看到了频道上下文。关于「{text[:120]}」："
            f"说下你要我具体做什么。"
        )
    return f"收到。你要我针对「{text[:120]}」做什么？"


def main():
    print(
        f"grokbot consumer polling every {POLL_INTERVAL}s "
        f"(REPLY_IN_THREAD={REPLY_IN_THREAD}, me={ME or 'auto-pending'}, ROOT={ROOT})",
        flush=True,
    )
    while True:
        try:
            msgs = peek()
            if msgs:
                sessions = load_sessions()
                for m in msgs:
                    # skip self if somehow queued
                    if ME and m.get("user") == ME:
                        sh(sys.executable, "inbox_ack.py", m["ts"])
                        continue
                    ch = m["channel"]
                    sess = sessions.setdefault(ch, [])
                    hist = channel_history(ch)
                    try:
                        reply = generate_reply(ch, m, sess, hist)
                        cmd = [sys.executable, "send.py", ch]
                        # Grok 默认顶层回复；仅当 REPLY_IN_THREAD 且有 thread_ts 才跟帖
                        if REPLY_IN_THREAD and m.get("thread_ts"):
                            cmd += ["--thread-ts", m["thread_ts"]]
                        r = sh(*cmd, input_text=reply)
                        if "sent ok: True" in r.stdout:
                            sess.append({"role": "user", "text": m["text"]})
                            sess.append({"role": "assistant", "text": reply})
                            sessions[ch] = sess[-40:]
                            sh(sys.executable, "inbox_ack.py", m["ts"])
                            print(f"replied in {m.get('channel_name', ch)}", flush=True)
                        else:
                            print(
                                f"send failed for {m['ts']}: {r.stdout} {r.stderr}",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        print(f"error handling {m.get('ts')}: {e}", file=sys.stderr)
                save_sessions(sessions)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
