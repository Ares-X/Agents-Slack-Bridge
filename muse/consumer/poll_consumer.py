"""消费层参考实现 A：轮询直回。

默认 generate_reply() 只是 echo 示例（mention 已脱敏），
生产使用必须换成真实模型调用，见 README §2.4。

发送状态机（consumer/send_state.json 持久化，防重复发送）：
  claim(发送前持久化领取) --发送--> ok --ack--> done（条目删除）
                                     |--ack 失败--> unacked（只重试 ack，
                                     |                        绝不重发正文）
                                     |--限流--> retry_wait（持久延期，到期原子重领）
                                     |--明确失败--> claim 释放，下轮干净重试
                                     └--结果不确定--> uncertain（经 history
                                        严格核验；核验无结论则延迟，绝不盲目
                                        重发；3 轮仍无结论转人工日志）

关键不变量：
- claim 在 spawn send.py 子进程之前落盘（fsync）。进程崩溃后重启，
  "sending" 条目一律转为 uncertain，永远不会恢复成"可直接发送"。
- send_reply 只有在"明确证明未发送"时返回 fail（API 明确拒绝 / 死在
  API 调用之前）；其余（超时、连接中断、输出含糊）一律 uncertain。
- verify_sent 必须同时核对：是我们自己的 bot、目标频道/线程一致、
  消息时间不早于本次发送尝试、全文精确匹配。证明不了就保持
  uncertain，绝不猜测成功。
- send_state.json 损坏绝不静默当成空状态：优先从 .bak/.tmp 恢复；
  都损坏则隔离为 send_state.json.corrupt.* 并抛 StateCorruptError，
  consumer 在此期间拒绝发送（fail-closed），直到人工恢复。

历史降级策略：history 拉取失败时延迟处理（3 轮），3 轮后降级进行，
在会话日志里留下可见标记，不向 Slack 泄露内部细节。

脚本 cwd：本文件在 consumer/ 下，inbox_peek / send / channel_history
都在上一层（muse/），因此 ROOT = dirname(BASE)，所有子进程在 ROOT 跑。
"""
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)  # muse/
SESSIONS_PATH = os.path.join(BASE, "channel_sessions.json")
STATE_PATH = os.path.join(BASE, "send_state.json")
POLL_INTERVAL = 30          # 秒
HISTORY_DEFER_LIMIT = 3     # history 连续失败这么多轮后降级进行
VERIFY_LIMIT = 3            # 发送结果不确定时，最多核验这么多轮
SEND_TIMEOUT = 60           # send.py 单次超时（秒）
VERIFY_SKEW_SECONDS = 60    # 核验时允许的本机/Slack 时钟偏差

from send_state import SendState, StateCorruptError

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


def text_hash(text):
    """回复全文的稳定指纹：核验用精确匹配，不再用 60 字前缀猜测。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def wire_text(text, mentions):
    """send.py 实际发出的最终正文：--mention 追加在正文末尾。

    全文 hash 必须对它计算，否则核验永远对不上。
    拼接规则与 send.py 保持一致（见该文件注释），改一处必须改另一处。
    """
    if mentions:
        text = text.rstrip() + " " + " ".join(f"<@{u}>" for u in mentions)
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


def _ack_safe(msg_ids):
    """ack() 的异常安全版本：子进程崩溃/超时抛异常时返回 False。

    重要：ack 抛异常绝不能让条目卡在 "sending"。发送已经成功（ok
    路径），只是确认动作本身不确定——调用方必须按"确认失败"处理，
    把条目记为 unacked（只重试 ack），而不是留在 sending 里等一次
    primary 损坏后的 stale .bak 恢复把它变回可发送。
    """
    try:
        return bool(ack(msg_ids))
    except Exception as e:
        print(f"ack subprocess error: {e}", file=sys.stderr)
        return False


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


_bot_identity = None


def resolve_bot_identity():
    """返回 (bot_id, bot_user_id)，任一可能为 None。进程内只解析一次。

    bot_id（B 开头）来自 .env 的 SLACK_BOT_ID；bot_user_id（U 开头）来自
    .env 的 SLACK_BOT_USER_ID，缺失时用 auth_test 解析一次。
    """
    global _bot_identity
    if _bot_identity is not None:
        return _bot_identity
    env_path = os.path.join(ROOT, ".env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    bot_id = env.get("SLACK_BOT_ID") or None
    bot_user_id = env.get("SLACK_BOT_USER_ID") or None
    token = env.get("SLACK_BOT_TOKEN")
    if not bot_user_id and token:
        try:
            import ssl as _ssl
            from slack_sdk.web import WebClient
            from net_config import read_proxy_config
            proxy, ca = read_proxy_config(env)
            ctx = _ssl.create_default_context(
                cafile=ca if ca and os.path.exists(ca) else None)
            c = WebClient(token=token,
                          **({"proxy": proxy} if proxy else {}), ssl=ctx)
            bot_user_id = c.auth_test().get("user_id") or None
        except Exception as e:
            print(f"bot identity resolve failed: {e}", file=sys.stderr)
    _bot_identity = (bot_id, bot_user_id)
    return _bot_identity


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


def send_reply(channel, text, thread_ts=None, mentions=(),
               client_msg_id=None):
    """返回 "ok" | "fail" | "uncertain" | ("retry_wait", retry_at)。

    只有"明确证明未发送"才返回 "fail"（API 明确拒绝 / 死在 API 调用
    之前）；限流单独返回 retry_wait，必须持久保存服务器的等待期限；
    其余一律 "uncertain"——请求可能已被 Slack 接受但响应丢失，
    调用方绝不能自动重发，必须走 history 核验。
    """
    cmd = [sys.executable, "send.py", channel]
    if thread_ts:
        cmd += ["--thread-ts", thread_ts]
    for u in mentions:
        cmd += ["--mention", u]
    if client_msg_id:
        cmd += ["--client-msg-id", client_msg_id]
    try:
        r = sh(*cmd, input_text=text, timeout=SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "uncertain"
    except Exception as e:
        print(f"send subprocess error: {e}", file=sys.stderr)
        return "uncertain"
    out = r.stdout or ""
    err = r.stderr or ""
    if r.returncode == 75:
        # 只接受完整、明确的延期结果；损坏输出不能当成可重试证明。
        try:
            result = json.loads(out)
            retry_at = result.get("retry_at")
            if (result.get("result") == "retry_wait"
                    and type(retry_at) in (int, float)
                    and math.isfinite(retry_at) and retry_at >= 0):
                return ("retry_wait", retry_at)
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
        return "uncertain"
    if "sent ok: True" in out:
        return "ok"
    if "sent ok: False" in out:
        return "fail"          # API 明确拒绝：证明未发送
    if "RESULT not-sent" in out or "RESULT not-sent" in err:
        return "fail"          # 死在 API 调用之前：证明未发送
    # 超时、连接中断（RESULT uncertain）、输出含糊：可能已发出
    return "uncertain"


def verify_sent(channel, thash, thread_ts="", bot_id=None, bot_user_id=None,
                sent_after=0.0, limit=30, client_msg_id=None):
    """经 history 严格核验本次发送是否已出现在频道里。

    候选必须同时满足（缺一不可）：
      1. 是 bot 消息，且身份是我们自己的 bot
         （bot_id 或 user id 命中其一）；
      2. 频道/线程一致（thread_ts 必须相等；顶层回复双方都为空）；
      3. 消息 ts 不早于本次发送尝试（允许 VERIFY_SKEW_SECONDS 时钟偏差）；
      4. 全文精确匹配（sha256，不再是 60 字前缀）；
      5. 携带与本次发送尝试相同的 client_msg_id——这是唯一能把
         history 里的一条消息关联到"这一次发送尝试"的证据。
         同身份、同正文和时间窗口不能单独证明成功（例如本 bot 在
         本次尝试前 30 秒发过相同正文，也会命中 1–4）。
    返回 True（已证实发出）/ False（无证据）/ None（历史不可用）。
    身份未知（bot_id/bot_user_id 都拿不到）时永远返回 False：
    证明不了就保持 uncertain，绝不猜测成功。
    本次尝试没有 client_msg_id（旧版本条目）时同样无法证明，
    返回 False。
    """
    msgs, err = channel_history(channel, limit=limit)
    if err:
        return None
    if not thash:
        return False
    if not client_msg_id:
        # 无法关联本次发送尝试：证明不了，保持 uncertain。
        return False
    for m in msgs:
        if not m.get("is_bot"):
            continue
        ident_ok = (bot_id and m.get("bot_id") == bot_id) or \
                   (bot_user_id and m.get("user") == bot_user_id)
        if not ident_ok:
            continue
        if (m.get("thread_ts") or "") != (thread_ts or ""):
            continue
        try:
            mts = float(m.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if mts < sent_after - VERIFY_SKEW_SECONDS:
            continue
        if m.get("text_sha256") != thash:
            continue
        if m.get("client_msg_id") != client_msg_id:
            continue
        return True
    return False


def _verify_hold(m, entry, state, bot_id, bot_user_id, why):
    """核验优先：能证明发出则 ack，否则保持 uncertain。绝不发送正文。

    用于 uncertain 条目、sending 残留条目、以及 claim 被并发抢占的
    情况——调用方不确定消息是否已发出时唯一的合法动作。
    """
    mid = m["msg_id"]
    # 恢复核验以发送尝试持久化的 channel/thread_ts 为权威：后续 CLI 调用
    # 不得改变核验目标，否则已存在的正确回执也无法确认（目标漂移）。
    ch = entry.get("channel") or m["channel"]
    if "thread_ts" in entry:
        thread_ts = entry.get("thread_ts") or ""
    else:
        thread_ts = m.get("thread_ts") or ""
    if entry.get("attempts", 0) >= VERIFY_LIMIT:
        print(f"MANUAL REVIEW needed: send result uncertain after "
              f"{VERIFY_LIMIT} verifications ({why}), {mid} left pending",
              file=sys.stderr)
        return "uncertain-held"
    v = verify_sent(ch, entry.get("text_hash"), thread_ts,
                    bot_id, bot_user_id, entry.get("claimed_at", 0.0),
                    client_msg_id=entry.get("client_msg_id"))
    if v is True:
        if _ack_safe([mid]):
            state.resolve(mid)
            return "verified-acked"
        state.set_unacked(mid)
        return "verified-unacked"
    state.set_uncertain(mid, entry.get("attempts", 0) + 1,
                        expected_client_msg_id=entry.get("client_msg_id"))
    print(f"send unverified ({why}; {entry.get('attempts', 0) + 1}/"
          f"{VERIFY_LIMIT}), holding {mid}")
    return "uncertain-held"


def _precheck(m, state, bot_id, bot_user_id):
    """本轮前置检查：返回非 None 表示本轮已处理完毕，调用方直接返回
    该结果字符串，不再继续。返回 None 表示可以进入领取/发送流程。

    覆盖：retry_wait 未到期 -> 不发送；unacked -> 只重试 ack；
    uncertain/sending -> 先严格核验，绝不盲目重发。
    """
    mid = m["msg_id"]
    # --- retry_wait：到期前不发送；unacked：只重试 ack ---
    entry = state.get(mid)
    if (entry and entry.get("status") == "retry_wait"
            and time.time() < entry["retry_at"]):
        return "retry-deferred"
    if entry and entry.get("status") == "unacked":
        if _ack_safe([mid]):
            state.resolve(mid)
            print(f"ack recovered for {mid}")
            return "acked-later"
        # ACK 仍失败：保持 unacked，绝不谎报成功（exit 3，经 EXIT 映射）
        return "sent-unacked"

    # --- uncertain / sending：先严格核验，不盲目重发 ---
    # sending 只有两种来源：另一个存活 consumer 正在发送，或上一轮死在
    # claim 与结果处理之间（重启时已转 uncertain，剩下的就是并发）。
    # 两种都不能直接重发，只能核验。
    if entry and entry.get("status") in ("uncertain", "sending"):
        return _verify_hold(
            m, entry, state, bot_id, bot_user_id,
            why=("previous attempt unfinished"
                 if entry.get("status") == "sending" else "uncertain result"))
    return None


def deliver_one(m, text, mentions, state, bot_id=None, bot_user_id=None):
    """单次投递：回复正文由调用方提供，与 handle_one 共享同一套
    防重复发送状态机。

    handle_one 用它投递模型生成的正文；真实消费链路（hook -> side
    chat -> agent，见 send_durable.py）用它投递 agent 的真实回复。
    前置检查、持久领取、限流延期、不确定保持的语义完全一致。

    m 只需要 msg_id / channel / thread_ts（agent 链路不传原文）。
    mentions 是显式点名的 user id 列表（可为空）。
    返回结果字符串，与 handle_one 相同。
    """
    r = _precheck(m, state, bot_id, bot_user_id)
    if r is not None:
        return r
    mid = m["msg_id"]
    ch = m["channel"]
    thread_ts = m.get("thread_ts") or ""

    # --- 领取、发送 ---
    # hash 必须对最终发出的正文计算（含 send.py 追加的显式 mention），
    # 否则核验时全文永远对不上。
    thash = text_hash(wire_text(text, mentions))
    # 本次发送尝试的唯一关联证据：随 POST 发给 Slack，history 回显后
    # 用于核验"这次发送"是否成功。同身份+同正文+时间窗口不能单独
    # 证明成功（30 秒前发过相同正文也会命中）。
    attempt_id = uuid.uuid4().hex
    # 先持久化领取（fsync），再 spawn 子进程：崩溃后重启走 uncertain，
    # 绝不直接重发。
    claim_entry, outcome = state.claim(mid, channel=ch, thread_ts=thread_ts,
                                       text_hash=thash,
                                       client_msg_id=attempt_id)
    if outcome == "completed":
        # 持久完成标记已存在：另一个 consumer 已经发送并 ack 了这条
        # 消息（本快照是旧的）。绝不再次发送。
        state.resolve(mid)  # 清理可能残留的旧条目
        print(f"{mid}: already completed by another consumer, skipping")
        return "already-acked"
    if outcome == "held":
        # TOCTOU：get 与 claim 之间被另一个存活 consumer 抢先领取。
        # 对方可能正在发送：本轮只核验，绝不并行发送。
        fresh = state.get(mid) or claim_entry
        if fresh.get("status") == "retry_wait":
            return "retry-deferred"
        return _verify_hold(m, fresh, state, bot_id, bot_user_id,
                            why="claim lost to concurrent consumer")
    result = send_reply(ch, text, thread_ts=thread_ts or None,
                        mentions=mentions, client_msg_id=attempt_id)

    if isinstance(result, tuple) and result[0] == "retry_wait":
        state.defer_retry(mid, result[1], client_msg_id=attempt_id)
        return "retry-deferred"
    if result == "ok":
        if _ack_safe([mid]):
            state.resolve(mid)
            return "replied"
        # 发送成功但确认失败：记 unacked，只重试 ack
        state.set_unacked(mid)
        return "sent-unacked"
    if result == "fail":
        # 已证明未发送：释放 claim，下轮干净重试
        state.resolve(mid)
        print(f"send failed for {mid}, will retry next round", file=sys.stderr)
        return "send-failed"
    # uncertain：立即严格核验一次，不盲目重发
    v = verify_sent(ch, thash, thread_ts, bot_id, bot_user_id,
                    claim_entry["claimed_at"], client_msg_id=attempt_id)
    if v is True:
        if _ack_safe([mid]):
            state.resolve(mid)
            return "verified-acked"
        state.set_unacked(mid)
        return "verified-unacked"
    state.set_uncertain(mid, 1,
                        expected_client_msg_id=claim_entry.get("client_msg_id"))
    print(f"send result uncertain for {mid}, holding for verification")
    return "uncertain-held"


def handle_one(m, sessions, state, bot_id=None, bot_user_id=None):
    mid = m["msg_id"]
    ch = m["channel"]

    # 前置检查（顺序与原来一致：先于 history，避免 retry_wait 等
    # 已决状态多一次 history 调用）。
    r = _precheck(m, state, bot_id, bot_user_id)
    if r is not None:
        return r

    # --- history 降级策略：失败先延迟，3 轮后降级进行并留可见标记 ---
    hist, herr = channel_history(ch)
    if herr:
        n = state.get_hist_deferred(mid) + 1
        state.set_hist_deferred(mid, n)
        if n < HISTORY_DEFER_LIMIT:
            print(f"deferring {mid}: history unavailable "
                  f"({n}/{HISTORY_DEFER_LIMIT}): {herr}")
            return "deferred"
        print(f"WARNING: proceeding degraded for {mid}: history unavailable "
              f"after {n} attempts: {herr}", file=sys.stderr)
        degraded_note = (f"[degraded] history unavailable after {n} attempts: "
                         f"{herr}")
    else:
        state.set_hist_deferred(mid, 0)
        degraded_note = None

    sess = sessions.setdefault(ch, [])
    if degraded_note:
        sess.append({"role": "system", "text": degraded_note})

    # --- 生成正文（参考实现只是 echo；生产换真实模型，见 README §2.4）---
    text, mentions = normalize_reply(generate_reply(ch, m, sess, hist))

    # --- 领取、发送、确认：与 agent 链路（send_durable.py）共享 deliver_one ---
    result = deliver_one(m, text, mentions, state, bot_id, bot_user_id)
    if result in ("replied", "sent-unacked"):
        sess.append({"role": "user", "text": m["text"]})
        sess.append({"role": "assistant", "text": text})
        sessions[ch] = sess[-40:]
    return result


def main():
    print(f"consumer polling every {POLL_INTERVAL}s (ROOT={ROOT}) ...")
    bot_id, bot_user_id = resolve_bot_identity()
    print(f"bot identity: bot_id={bot_id or '?'} "
          f"bot_user_id={bot_user_id or '?'}")
    state = None
    while True:
        try:
            if state is None:
                # claim 需要读取 inbox 的持久完成标记（tombstone）：
                # 锁顺序恒为 send_state -> inbox，见 send_state.py。
                state = SendState(STATE_PATH,
                                  inbox_path=os.path.join(ROOT,
                                                          "inbox.jsonl"),
                                  inbox_lock_path=os.path.join(ROOT,
                                                               "inbox.lock"))
            state.ensure_usable()
            msgs = peek()
            if msgs:
                sessions = load_json(SESSIONS_PATH, {})
                for m in msgs:
                    try:
                        res = handle_one(m, sessions, state,
                                         bot_id=bot_id,
                                         bot_user_id=bot_user_id)
                        print(f"{m['msg_id']}: {res}")
                    except StateCorruptError:
                        raise
                    except Exception as e:
                        print(f"error handling {m.get('msg_id')}: {e}",
                              file=sys.stderr)
                save_json(SESSIONS_PATH, sessions)
        except StateCorruptError as e:
            # fail-closed：状态不可用时拒绝发送，直到人工恢复。
            state = None
            print(f"CRITICAL: send state unusable, refusing to send: {e}",
                  file=sys.stderr)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
