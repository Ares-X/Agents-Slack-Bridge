"""消费层参考实现 A：轮询直回。

默认 generate_reply() 只是 echo 示例（mention 已脱敏），
生产使用必须换成真实模型调用，见 README §2.4。

发送状态机（consumer/send_state.json 持久化，防重复发送）：
  pending --发送--> sent_ok --ack--> done
                     |           \u2514 ack 失败 -> sent_unacked（只重试 ack，绝不重发正文）
                     |--明确失败--> pending（下轮重试）
                     \u2514--结果不确定--> uncertain（经 history 核验；核验无结论则延迟，
                                          绝不盲目重发；3 轮仍无结论转人工日志）

历史降级策略：history 拉取失败时延迟处理（3 轮），3 轮后降级进行，
在会话日志里留下可见标记，不向 Slack 泄露内部细节。

脚本 cwd：本文件在 consumer/ 下，inbox_peek / send / channel_history
都在上一层（muse/），因此 ROOT = dirname(BASE)，所有子进程在 ROOT 跑。
"""
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)  # muse/
SESSIONS_PATH = os.path.join(BASE, "channel_sessions.json")
STATE_PATH = os.path.join(BASE, "send_state.json")
POLL_INTERVAL = 30          # 秒
HISTORY_DEFER_LIMIT = 3     # history 连续失败这么多轮后降级进行
VERIFY_LIMIT = 3            # 发送结果不确定时，最多核验这么多轮
SEND_TIMEOUT = 60           # send.py 单次超时（秒）

MENTION_RE = re.compile(r"<@([UB][A-Z0-9]+)>")
CHANNEL_REF_RE = re.compile(r"<#(C[A-Z0-9]+)\|([^>]+)>")
SPECIAL_MENTION_RE = re.compile(r"<!([a-zA-Z_]+)>")


def strip_mentions(text):
    """把真实点名转成纯文本（不再触发通知）。

    <@U123> -> @U123；<#C123|general> -> #general；<!channel> -> @channel。
    纯文本 @UID 不会产生 Slack 通知。主动点名请走 generate_reply 的
    (text, [uid...]) 返回形式，经 send.py --mention 显式发出。
    """
    text = MENTION_RE.sub(r"@\1", text)
    text = CHANNEL_REF_RE.sub(r"#\2", text)
    text = SPECIAL_MENTION_RE.sub(r"@\1", text)
    return text


def sh(*args, input_text=None, timeout=120):
    return subprocess.run(args, input=input_text, capture_output=True,
                          text=True, cwd=ROOT, timeout=timeout)


def peek():
    r = sh(sys.executable, "inbox_peek.py")
    out = []
    for line in r.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def ack(msg_ids):
    """返回 True=确认成功。失败时调用方不得视为已确认、不得重发正文。"""
    r = sh(sys.executable, "inbox_ack.py", *msg_ids)
    ok = r.returncode == 0
    if not ok:
        print(f"ACK FAILED for {msg_ids}: {r.stdout} {r.stderr}",
              file=sys.stderr)
    return ok


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def channel_history(channel, limit=15):
    """返回 (messages, error)。error 非空时调用方必须走降级/延迟策略，
    不可当成空历史静默处理。"""
    r = sh(sys.executable, "channel_history.py", channel, str(limit))
    msgs, err = [], None
    for line in r.stdout.splitlines():
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "error" in m:
            err = m.get("reason") or m["error"]
        else:
            msgs.append(m)
    if r.returncode != 0 and err is None:
        err = f"exit={r.returncode} {r.stderr.strip()[:200]}"
    return msgs, err


def generate_reply(channel, message, session, history):
    """★ 换成你家 agent 的真实模型调用。

    返回 str（正文）或 (str, [uid...])（正文 + 显式点名）。
    默认 echo 示例：mention 已脱敏，不会误触发其他 bot。
    """
    return f"收到：{strip_mentions(message['text'])[:200]}"


def normalize_reply(ret):
    """统一成 (text, [mention_uids])。"""
    if isinstance(ret, tuple):
        text, uids = ret[0], list(ret[1] or [])
    else:
        text, uids = ret, []
    return text, uids


def send_reply(channel, text, thread_ts=None, mentions=()):
    """返回 "ok" | "fail" | "uncertain"。"""
    cmd = [sys.executable, "send.py", channel]
    if thread_ts:
        cmd += ["--thread-ts", thread_ts]
    for u in mentions:
        cmd += ["--mention", u]
    try:
        r = sh(*cmd, input_text=text, timeout=SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "uncertain"
    except Exception as e:
        print(f"send subprocess error: {e}", file=sys.stderr)
        return "uncertain"
    if r.returncode == 0 and "sent ok: True" in r.stdout:
        return "ok"
    if r.returncode != 0 or "sent ok: False" in r.stdout:
        return "fail"
    return "uncertain"  # 输出含糊：无法判定是否发出


def verify_sent(channel, text):
    """经 history 核验正文是否已发出。返回 True/False/None（无法核验）。"""
    msgs, err = channel_history(channel, limit=10)
    if err:
        return None
    needle = text.strip()[:60]
    for m in msgs:
        if m.get("is_bot") and (m.get("text") or "").strip().startswith(needle):
            return True
    return False


def handle_one(m, sessions, state):
    mid = m["msg_id"]
    ch = m["channel"]

    # --- sent_unacked：只重试 ack，绝不重发正文 ---
    if mid in state.get("sent_unacked", []):
        if ack([mid]):
            state["sent_unacked"].remove(mid)
            print(f"ack recovered for {mid}")
        return "acked-later"

    # --- uncertain：先核验，不盲目重发 ---
    unc = state.get("uncertain", {}).get(mid)
    if unc:
        if unc.get("attempts", 0) >= VERIFY_LIMIT:
            print(f"MANUAL REVIEW needed: send result uncertain after "
                  f"{VERIFY_LIMIT} verifications, {mid} left pending",
                  file=sys.stderr)
            return "uncertain-held"
        v = verify_sent(ch, unc.get("text", ""))
        if v is True:
            if ack([mid]):
                state["uncertain"].pop(mid, None)
                return "verified-acked"
            state["sent_unacked"].append(mid)
            return "verified-unacked"
        unc["attempts"] = unc.get("attempts", 0) + 1
        unc["last"] = time.time()
        print(f"send unverified ({unc['attempts']}/{VERIFY_LIMIT}), holding {mid}")
        return "uncertain-held"

    # --- history 降级策略：失败先延迟，3 轮后降级进行并留可见标记 ---
    hist, herr = channel_history(ch)
    if herr:
        n = state.get("hist_deferred", {}).get(mid, 0) + 1
        state.setdefault("hist_deferred", {})[mid] = n
        if n < HISTORY_DEFER_LIMIT:
            print(f"deferring {mid}: history unavailable "
                  f"({n}/{HISTORY_DEFER_LIMIT}): {herr}")
            return "deferred"
        print(f"WARNING: proceeding degraded for {mid}: history unavailable "
              f"after {n} attempts: {herr}", file=sys.stderr)
        degraded_note = (f"[degraded] history unavailable after {n} attempts: "
                         f"{herr}")
    else:
        state.get("hist_deferred", {}).pop(mid, None)
        degraded_note = None

    sess = sessions.setdefault(ch, [])
    if degraded_note:
        sess.append({"role": "system", "text": degraded_note})

    # --- 生成并发送 ---
    text, mentions = normalize_reply(generate_reply(ch, m, sess, hist))
    result = send_reply(ch, text, thread_ts=m.get("thread_ts") or None,
                        mentions=mentions)

    if result == "ok":
        if ack([mid]):
            sess.append({"role": "user", "text": m["text"]})
            sess.append({"role": "assistant", "text": text})
            sessions[ch] = sess[-40:]
            return "replied"
        # 发送成功但确认失败：记 sent_unacked，只重试 ack
        state.setdefault("sent_unacked", []).append(mid)
        sess.append({"role": "user", "text": m["text"]})
        sess.append({"role": "assistant", "text": text})
        sessions[ch] = sess[-40:]
        return "sent-unacked"
    if result == "fail":
        print(f"send failed for {mid}, will retry next round", file=sys.stderr)
        return "send-failed"
    # uncertain：立即核验一次，不盲目重发
    v = verify_sent(ch, text)
    if v is True:
        if ack([mid]):
            return "verified-acked"
        state.setdefault("sent_unacked", []).append(mid)
        return "verified-unacked"
    state.setdefault("uncertain", {})[mid] = {
        "attempts": 1, "last": time.time(), "text": text[:200]}
    print(f"send result uncertain for {mid}, holding for verification")
    return "uncertain-held"


def main():
    print(f"consumer polling every {POLL_INTERVAL}s (ROOT={ROOT}) ...")
    while True:
        try:
            msgs = peek()
            if msgs:
                sessions = load_json(SESSIONS_PATH, {})
                state = load_json(STATE_PATH, {})
                for m in msgs:
                    try:
                        res = handle_one(m, sessions, state)
                        print(f"{m['msg_id']}: {res}")
                    except Exception as e:
                        print(f"error handling {m.get('msg_id')}: {e}",
                              file=sys.stderr)
                save_json(SESSIONS_PATH, sessions)
                save_json(STATE_PATH, state)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
