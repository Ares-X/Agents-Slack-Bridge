"""Grok Bot 消费层：~5s 轮询。

Modes:
  - agent_wake (default when webhook.env present, or REPLY_MODE=agent_wake):
    escalate stale sending; ack-only for reply_status=sent; respect rate_limited;
    for NEW claimable messages do NOT template-send — run pending_notify.py then
    wake_agent.py (debounced by claimable fingerprint).
  - template (ONLY when REPLY_MODE=template explicitly):
    context-aware template stub via generate_reply + process_one.
  - error (missing webhook.env and REPLY_MODE not set/invalid):
    refuse to start; keep inbox messages (no silent template).

多 agent 协作默认（可用 .env 覆盖）：
  - SLACK_BRIDGE_POLL_SEC=5
  - REPLY_IN_THREAD=0 → 频道顶层回复
  - REPLY_IN_THREAD=1 → 跟帖：thread_ts 优先，否则消息 ts
  - 本 bot user ID：SLACK_BOT_USER_ID 或 auth_test

发送状态机（reply_pipeline，与 pending fallback 共用）：
  - 先 durable claim (sending)，失败则不 send
  - 仅当输出证明未发送 (not_sent: / sent ok: False) 才 retryable
  - 其余失败 → uncertain；sending/uncertain 永不盲发
  - sent → 只重试 ack
  - 启动时 escalate stale sending → uncertain
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
WEBHOOK_ENV_PATH = os.path.join(ROOT, "webhook.env")
WAKE_DEBOUNCE_SEC = 45.0

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from inbox_store import (  # noqa: E402
    ack_keys,
    escalate_stale_sending,
    is_send_ready,
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


def resolve_reply_mode(env=None, webhook_env_path=None):
    """Resolve reply mode. Template ONLY if REPLY_MODE=template explicitly.

    - REPLY_MODE=template → "template"
    - REPLY_MODE=agent_wake → "agent_wake"
    - REPLY_MODE unset/empty + webhook.env present → "agent_wake"
    - otherwise (missing webhook.env, or invalid REPLY_MODE) → "error"
      (caller must keep messages; never silent-fall-back to template)
    """
    env = _ENV if env is None else env
    wh = WEBHOOK_ENV_PATH if webhook_env_path is None else webhook_env_path
    raw = (
        os.environ.get("REPLY_MODE")
        or (env or {}).get("REPLY_MODE")
        or ""
    ).strip().lower()
    if raw == "template":
        return "template"
    if raw == "agent_wake":
        return "agent_wake"
    if raw in ("",) and os.path.exists(wh):
        return "agent_wake"
    return "error"


REPLY_MODE = resolve_reply_mode()

# Bounded wake-failure backoff (seconds): 5, 10, 20, 40, cap 60
WAKE_BACKOFF_BASE = 5.0
WAKE_BACKOFF_CAP = 60.0


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
    """短上下文感知模板回复（stub，非 Grok 模型接线）。仅 REPLY_MODE=template。"""
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


def claimable_fingerprint(msgs):
    """Stable fingerprint of claimable (channel,ts) set."""
    keys = sorted(
        f"{m.get('channel') or ''}:{m.get('ts') or ''}"
        for m in msgs
    )
    return "|".join(keys), frozenset(keys)


def handle_ack_and_skips(m, *, runner=None):
    """Handle self-ack, sent ack-only, uncertain/sending skip, rate_limited wait.

    Returns:
      ("done", False) — handled / skip, no further action
      ("claimable", False) — ready for agent wake (or template send)
    """
    if ME and m.get("user") == ME:
        ack_keys(INBOX_PATH, {msg_key(m)})
        return "done", False

    ch = m.get("channel") or ""
    ts = m.get("ts") or ""
    label = m.get("channel_name") or ch
    status = m.get("reply_status")

    if status == "sent":
        result = process_one(
            INBOX_PATH, m, "", reply_in_thread=REPLY_IN_THREAD,
            root=ROOT, runner=runner,
        )
        if result.get("outcome") == "acked":
            print(f"acked prior send in {label} ({ch}:{ts})", flush=True)
        return "done", False

    if status in ("uncertain", "sending"):
        print(
            f"skip {status} for {ch}:{ts} (will not resend; verify manually)",
            file=sys.stderr,
        )
        return "done", False

    if status == "rate_limited":
        try:
            until = float(m.get("retry_after_until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        now = time.time()
        if now < until:
            print(
                f"rate-limited wait for {ch}:{ts}: "
                f"{until - now:.1f}s remaining; not sending",
                file=sys.stderr,
            )
            return "done", False
        # Wait expired → claimable again

    if not is_send_ready(m):
        print(
            f"skip not-send-ready for {ch}:{ts} status={status!r}",
            file=sys.stderr,
        )
        return "done", False

    return "claimable", False


def handle_one_template(m, sessions, *, runner=None):
    """Process one undelivered message via template + reply_pipeline."""
    kind, _ = handle_ack_and_skips(m, runner=runner)
    if kind != "claimable":
        return False

    ch = m.get("channel") or ""
    ts = m.get("ts") or ""
    label = m.get("channel_name") or ch

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


# Back-compat alias for tests that call handle_one
def handle_one(m, sessions, *, runner=None):
    return handle_one_template(m, sessions, runner=runner)


def _parse_retry_after_sec(stdout: str, stderr: str = "") -> float | None:
    """Parse wake_agent machine line retry_after_sec=N from stdout/stderr."""
    import re as _re
    blob = (stdout or "") + "\n" + (stderr or "")
    m = _re.search(r"retry_after_sec=([0-9]+(?:\.[0-9]+)?)", blob)
    if not m:
        return None
    try:
        return max(0.0, float(m.group(1)))
    except ValueError:
        return None


def _wake_backoff_sec(fail_count: int) -> float:
    """Bounded exponential backoff: 5, 10, 20, 40, ... cap WAKE_BACKOFF_CAP."""
    n = max(1, int(fail_count))
    return min(WAKE_BACKOFF_CAP, WAKE_BACKOFF_BASE * (2 ** (n - 1)))


def maybe_wake_agent(claimable, wake_state, *, time_fn=None, runner=None):
    """Run pending_notify + wake_agent with fingerprint debounce + failure waits.

    wake_state keys:
      last_fp, last_keys, last_at — success fingerprint debounce
      wake_retry_after_until — absolute epoch from webhook 429 Retry-After
      wake_backoff_until — absolute epoch for other wake failures
      wake_fail_count — consecutive non-success wakes (reset on success)

    Debounce rules:
      - DO post when claimable set gains new (channel,ts) keys
      - do NOT re-POST the same set within WAKE_DEBOUNCE_SEC
      - after debounce window, same set may re-POST (agent may have missed)
      - shrink-only changes within debounce are skipped
    Failure rules:
      - 429 → honor Retry-After (do not count as success; no 5s hammer)
      - other failures → bounded backoff; not success
    """
    if not claimable:
        return wake_state

    now = float((time_fn or time.time)())
    ra_until = float(wake_state.get("wake_retry_after_until") or 0)
    if now < ra_until:
        print(
            f"agent_wake: skip webhook waiting Retry-After "
            f"{ra_until - now:.1f}s remaining (n={len(claimable)})",
            flush=True,
        )
        return wake_state
    bo_until = float(wake_state.get("wake_backoff_until") or 0)
    if now < bo_until:
        print(
            f"agent_wake: skip webhook waiting failure backoff "
            f"{bo_until - now:.1f}s remaining (n={len(claimable)})",
            flush=True,
        )
        return wake_state

    fp, keys = claimable_fingerprint(claimable)
    last_fp = wake_state.get("last_fp") or ""
    last_keys = wake_state.get("last_keys") or frozenset()
    last_at = float(wake_state.get("last_at") or 0)
    gained_keys = keys - last_keys
    gained = bool(gained_keys)
    age = now - last_at if last_at else None

    if not gained and fp == last_fp and age is not None and age < WAKE_DEBOUNCE_SEC:
        print(
            f"agent_wake: skip webhook same fingerprint within "
            f"{WAKE_DEBOUNCE_SEC:.0f}s (n={len(claimable)})",
            flush=True,
        )
        return wake_state

    if (
        not gained
        and fp != last_fp
        and last_keys
        and age is not None
        and age < WAKE_DEBOUNCE_SEC
    ):
        print(
            f"agent_wake: skip webhook shrink-only fingerprint within "
            f"{WAKE_DEBOUNCE_SEC:.0f}s (n={len(claimable)})",
            flush=True,
        )
        out = dict(wake_state)
        out["last_fp"] = fp
        out["last_keys"] = keys
        # keep original last_at / failure clocks
        return out

    run = runner if runner is not None else sh

    r_notify = run(sys.executable, "pending_notify.py")
    if getattr(r_notify, "returncode", 1) != 0:
        print(
            f"agent_wake: pending_notify failed rc={r_notify.returncode}: "
            f"{(r_notify.stderr or r_notify.stdout or '')[:200]}",
            file=sys.stderr,
        )
        # notify failure: bounded backoff, not success
        fails = int(wake_state.get("wake_fail_count") or 0) + 1
        delay = _wake_backoff_sec(fails)
        out = dict(wake_state)
        out["wake_fail_count"] = fails
        out["wake_backoff_until"] = now + delay
        print(
            f"agent_wake: failure backoff {delay:.0f}s "
            f"(fail_count={fails}; not success)",
            file=sys.stderr,
        )
        return out

    r_wake = run(sys.executable, "wake_agent.py")
    rc = getattr(r_wake, "returncode", 1)
    stdout = getattr(r_wake, "stdout", "") or ""
    stderr = getattr(r_wake, "stderr", "") or ""
    if rc == 0:
        reason = "gained keys" if gained else (
            "debounce expired" if fp == last_fp else "fingerprint changed"
        )
        print(
            f"agent_wake: posted webhook for {len(claimable)} claimable "
            f"({reason})",
            flush=True,
        )
        return {
            "last_fp": fp,
            "last_keys": keys,
            "last_at": now,
            "wake_fail_count": 0,
            "wake_retry_after_until": 0.0,
            "wake_backoff_until": 0.0,
        }

    # Failure — must NOT count as success (do not set last_at/fp as posted)
    ra = _parse_retry_after_sec(stdout, stderr)
    out = dict(wake_state)
    if ra is not None and (rc == 4 or "429" in stderr or "rate limited" in stderr.lower()):
        out["wake_retry_after_until"] = now + float(ra)
        print(
            f"agent_wake: wake_agent 429; honor Retry-After {ra}s "
            f"(not success; n={len(claimable)})",
            file=sys.stderr,
        )
        return out

    fails = int(wake_state.get("wake_fail_count") or 0) + 1
    delay = _wake_backoff_sec(fails)
    # Prefer explicit retry_after from body if present even on non-429
    if ra is not None:
        delay = max(delay, float(ra))
    out["wake_fail_count"] = fails
    out["wake_backoff_until"] = now + delay
    print(
        f"agent_wake: wake_agent failed rc={rc}: {(stderr or stdout)[:200]}",
        file=sys.stderr,
    )
    print(
        f"agent_wake: failure backoff {delay:.0f}s "
        f"(fail_count={fails}; not success)",
        file=sys.stderr,
    )
    return out


def poll_agent_wake_once(wake_state, *, runner=None):
    """One poll cycle in agent_wake mode. Returns updated wake_state."""
    msgs = peek()
    if not msgs:
        return wake_state
    claimable = []
    for m in msgs:
        kind, _ = handle_ack_and_skips(m, runner=runner)
        if kind == "claimable":
            claimable.append(m)
    if claimable:
        wake_state = maybe_wake_agent(claimable, wake_state)
    else:
        print("agent_wake: no claimable this round (acks/skips only)", flush=True)
    return wake_state


def main():
    n = escalate_stale_sending(INBOX_PATH)
    if n:
        print(f"escalated {n} stale sending → uncertain", flush=True)

    mode = resolve_reply_mode()
    global REPLY_MODE
    REPLY_MODE = mode

    if mode == "error":
        raw = (
            os.environ.get("REPLY_MODE")
            or _ENV.get("REPLY_MODE")
            or ""
        ).strip()
        print(
            "ERROR: reply mode unresolved — set REPLY_MODE=template or "
            "REPLY_MODE=agent_wake, or provide webhook.env for agent_wake. "
            f"Got REPLY_MODE={raw!r}, webhook.env="
            f"{'present' if os.path.exists(WEBHOOK_ENV_PATH) else 'missing'}. "
            "Refusing silent template fallback; inbox messages kept. Exiting.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(
        f"grokbot consumer polling every {POLL_INTERVAL}s "
        f"mode={mode} "
        f"(REPLY_IN_THREAD={REPLY_IN_THREAD}, me={ME or 'auto-pending'}, ROOT={ROOT})",
        flush=True,
    )
    if mode == "template":
        print(
            "NOTE: generate_reply() is a template stub — not auto-wired to Grok models.",
            flush=True,
        )
    else:
        print(
            "agent_wake: claimable messages wake Grok Bot via webhook "
            "(no template send); see AGENT_WAKE.md",
            flush=True,
        )

    wake_state = {
        "last_fp": "",
        "last_keys": frozenset(),
        "last_at": 0.0,
        "wake_fail_count": 0,
        "wake_retry_after_until": 0.0,
        "wake_backoff_until": 0.0,
    }
    while True:
        try:
            if mode == "agent_wake":
                wake_state = poll_agent_wake_once(wake_state)
            elif mode == "template":
                msgs = peek()
                if msgs:
                    sessions = load_sessions()
                    for m in msgs:
                        handle_one_template(m, sessions)
                    save_sessions(sessions)
            else:
                print(f"poll skip: unknown mode={mode!r}", file=sys.stderr)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
