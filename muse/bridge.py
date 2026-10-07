"""Slack Socket Mode -> local inbox queue. 只收不发。

监听 bot 的私信（message.im）、频道 @mention（app_mention），
以及人类用户在 bot 自己消息的 thread 下的回复（thread_reply，
bot 的 thread 回复不入队，防回环），
把每条消息追加到 inbox.jsonl（见 inbox_store.py：append-only + tombstone ack），
供消费层读取处理。发送请用 send.py。

可靠性顺序（关键）：
  1. 先把事件可靠落盘（flush + fsync），
  2. 再向 Slack 发 ACK，
  3. 名称查询等慢操作一律不在热路径（见 resolve.py，消费层按需调用）。
落盘失败则不 ACK，靠 Slack 重发 + msg_id 去重实现 at-least-once。

配置：同目录 .env（0600），见 .env.example。
依赖：pip install slack_sdk
"""
import logging
import os
import signal
import ssl
import sys
import threading
import time

import inbox_store

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")
LOG_PATH = os.path.join(BASE, "bridge.log")


def load_env(path):
    d = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


_ENV = load_env(ENV_PATH) if os.path.exists(ENV_PATH) else {}
# 代理/CA 经 net_config 统一读取（issue #7）：.env > 标准环境变量 > 直连。
# 仓库通用版不硬编码内网地址，部署值放在 .env，不禁用 TLS 验证。
from net_config import read_proxy_config
PROXY_URL, CA_BUNDLE = read_proxy_config(_ENV)

# 多 agent 协作默认：放行其他 bot 的 @mention（自己的消息永远过滤，防自循环）。
# 可选白名单：.env 里 ALLOWED_BOT_USERS / ALLOWED_BOT_IDS（逗号分隔）。
# 两者都留空 = 允许任意其他 bot（协作默认）；任一非空 = 白名单模式，仅放行列出的 ID。
def _parse_csv_set(raw):
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


ALLOWED_BOT_USERS = _parse_csv_set(_ENV.get("ALLOWED_BOT_USERS", ""))
ALLOWED_BOT_IDS = _parse_csv_set(_ENV.get("ALLOWED_BOT_IDS", ""))
WHITELIST_MODE = bool(ALLOWED_BOT_USERS or ALLOWED_BOT_IDS)

# DM 里明确支持的消息子类型；其他（如 message_changed / message_deleted /
# channel_join 等）一律只 ACK 不入队，并在日志里可见。
SUPPORTED_IM_SUBTYPES = frozenset({None, "me_message"})

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def _proxy_kw():
    return {"proxy": PROXY_URL} if PROXY_URL else {}


def _ssl_ctx():
    return ssl.create_default_context(
        cafile=CA_BUNDLE if CA_BUNDLE and os.path.exists(CA_BUNDLE) else None)


def classify_event(event):
    """Return "mention" | "dm" | None. Pure; no I/O.

    Explicitly enumerates supported message subtypes so that edits,
    deletes and other housekeeping events are never mistaken for new
    messages.
    """
    etype = event.get("type")
    if etype == "app_mention":
        return "mention"
    if etype == "message" and event.get("channel_type") == "im":
        subtype = event.get("subtype")
        if subtype not in SUPPORTED_IM_SUBTYPES:
            return None  # e.g. message_changed / message_deleted: ignore
        return "dm"
    return None


def is_own_message(event, me):
    return bool(me) and event.get("user") == me


def is_bot_message(event):
    return bool(event.get("bot_id")) or event.get("subtype") == "bot_message"


def is_human_thread_reply_candidate(event, me):
    """频道里人类用户的 thread 回复（非私信、非自己、非 bot）。

    是否入队还要看 thread 的父消息是不是我们自己发的，
    那个判断需要一次 Slack API 查询，不能放在纯函数里，
    由 handle() 先 ACK 再做 best-effort 检查。
    """
    if event.get("type") != "message":
        return False
    if event.get("channel_type") == "im":
        return False  # 私信里的人类消息本来就会入队
    if not event.get("thread_ts"):
        return False
    if event.get("subtype") not in (None, "me_message"):
        return False
    if is_own_message(event, me):
        return False
    if is_bot_message(event):
        return False  # bot 的 thread 回复不唤醒，防回环
    return True


def bot_allowed(event):
    """Other bots: default-allow (collab), or allowlist-only in whitelist mode."""
    if not WHITELIST_MODE:
        return True
    return event.get("user") in ALLOWED_BOT_USERS \
        or event.get("bot_id") in ALLOWED_BOT_IDS


def build_record(event, kind):
    """Minimal record. No API calls here -- name resolution is the
    consumer's job (resolve.py), never on the ACK-critical path."""
    channel = event.get("channel", "")
    ts = event.get("ts", "")
    return {
        "msg_id": inbox_store.msg_id(channel, ts),
        "channel": channel,
        "user": event.get("user", ""),
        "text": event.get("text", ""),
        "kind": kind,                            # dm | mention | thread_reply
        "ts": ts,
        "thread_ts": event.get("thread_ts", ""),  # 原帖回复用
        "received_at": time.time(),
    }


def process_event(event, me):
    """Pure decision step (testable without Slack).

    Returns ("queue", record) | ("check_thread_parent", event)
    | ("ack_only", reason).
    """
    kind = classify_event(event)
    if kind is None:
        if is_human_thread_reply_candidate(event, me):
            # 人类在频道 thread 里的回复：要不要入队取决于父消息
            # 是不是我们自己发的，handle() 里查完再决定。
            return ("check_thread_parent", event)
        return ("ack_only", "not-subscribed-or-unsupported-subtype")
    if is_own_message(event, me):
        return ("ack_only", "own-message")
    if is_bot_message(event) and not bot_allowed(event):
        return ("ack_only", "bot-not-allowlisted")
    return ("queue", build_record(event, kind))


def resolve_thread_reply(event, me, my_bot_id, web, append):
    """判定一条频道 thread 回复是否入队（含父消息查询与落盘）。

    返回 ("queued", record) | ("drop", None) | ("retry", None)：
      queued -- 父消息确认是自己发的，且已可靠落盘，可以 ACK
      drop   -- 父消息确认不是自己发的，可以 ACK（丢弃）
      retry  -- 查询失败 / 父消息不明 / 落盘失败，不可 ACK，等 Slack 重发

    防丢消息设计：
      * 先查、再存、最后 ACK：任何一步失败都不 ACK，靠 Slack
        at-least-once 重发来重试；查询与落盘都是幂等的，重发不重复入队。
      * "确认不是" 与 "查不到" 严格区分：超时、限流、父消息缺失一律按
        retry 处理，绝不当成非目标消息丢弃。
      * append 抛异常（落盘/fsync 失败）-> retry；重复事件由 append
        内部确认 fsync 后返回 False，照常 ACK。

    web 需提供 conversations_replies(channel, ts, limit, inclusive)；
    append(record) 负责落盘，抛异常表示失败。
    """
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts", "")
    try:
        resp = web.conversations_replies(channel=channel, ts=thread_ts,
                                         limit=1, inclusive=True)
    except Exception:
        logging.exception("thread parent check failed for %s:%s, will retry",
                          channel, thread_ts)
        return ("retry", None)
    msgs = (resp or {}).get("messages") or []
    if not msgs:
        logging.warning("thread parent not found for %s:%s, will retry",
                        channel, thread_ts)
        return ("retry", None)
    parent = msgs[0]
    is_mine = (me and parent.get("user") == me) or \
              (my_bot_id and parent.get("bot_id") == my_bot_id)
    if not is_mine:
        return ("drop", None)
    record = build_record(event, "thread_reply")
    try:
        append(record)
    except Exception:
        logging.exception("thread reply persist failed for %s, will retry",
                          record.get("msg_id"))
        return ("retry", None)
    return ("queued", record)


# --- 自愈监督层 (2026-10-05) ---
# 背景: 出站 socket 常被代理掐断 (SSLEOFError)。SDK 自带 auto_reconnect
# (slack_sdk 3.45.0, 默认开启) 能处理 CLOSE 事件与会话失效 (实测 10-05
# 01:03 一次 SSLEOFError 后 SDK 自行重建会话); 但构造后 connect() 失败、
# 或异常逃出 client 生命周期时, 进程会直接退出, 之前只能靠外部保活重启。
#
# 覆盖范围 (按 slack_sdk 3.45.0 实测源码, builtin/client.py):
#   * 主线程 client 生命周期内的任何失败: connect() 抛异常 (代理/SSL/
#     握手失败)、client 意外返回、SDK 内部 sys.exit —— 全部触发重建。
#     构造即起的后台线程 (IntervalRunner) 与线程池由 finally 中的 close()
#     释放, 连续 connect 失败不会在进程里越积越多。
#   * 干净停机: SIGTERM / SIGINT 的 handler 内只做一次布尔赋值
#     (_shutdown = True), 不碰 logging/锁/IO —— 信号可能正好打断同个
#     stream 的写操作, 在 handler 里做 I/O 会导致 reentrant call
#     (2026-10-05 单测实测捕获)。各阻塞阶段以 1 秒粒度响应 _shutdown,
#     信号到达后最多约 1 秒即干净退出, 不再重建 client, 无 traceback。
#   * 信号级死亡 (SIGKILL 等): 由 systemd Restart=always 兜底。
# 不覆盖 (如实说明, 未验证):
#   * SDK 后台线程静默死亡 (主线程仍在 sleep, 表现为连接卡死): 本层
#     检测不到; 目前唯一防线是 SDK 自己的会话监控与重连。
#   * 长连接无 CLOSE 事件的半死状态: 同上, 依赖 SDK 重连。
#
# 停机上限分阶段 (均有单测覆盖, 见 tests/test_bridge_shutdown_phases.py):
#   * 退避等待 / 主监听循环: 1 秒粒度轮询, 信号后最多约 1 秒退出。
#   * SDK 限流重试: apps.connections.open 返回 ratelimited 时, SDK 原生
#     实现会 time.sleep(Retry-After) 后递归重试, 信号来了也停不下来。
#     这里用实例补丁换成 _issue_new_wss_url_bounded: Retry-After 以 1 秒
#     粒度等待, 信号后最多约 1 秒抛 _ShutdownRequested, 停机后不再继续
#     调用 apps.connections.open。补丁挂在 client 实例上, 初始 connect()
#     与 SDK 后台重连线程 (monitor -> connect_to_new_endpoint) 都走它。
#   * connect() 握手: _connect_bounded 放到 daemon 线程跑, 主线程 1 秒
#     粒度 join; 信号后最多约 1 秒放弃等待, 由 finally 做 close() 释放。
#   * close(): SDK 原生 close() 会无超时 join 后台线程; _close_bounded
#     加 CLOSE_TIMEOUT_SECS (15s) 上限, 超时则记 error 日志并放弃
#     (最终进程退出由看门狗和服务监管兜底)。
#   * 进程级最终停机上限: _close_bounded 只限制主线程等待。若 SDK
#     ThreadPoolExecutor 里有卡住的任务,
#     Python 退出钩子仍会等它, 进程退不掉。最终停机看门狗在
#     收到停机请求或监督循环决定退出后计时, FINAL_DEADLINE_SECS (30s)
#     后仍未退出则 os._exit(1) 硬结束 —— 实际进程结束, 有单测覆盖。
#     恢复语义不受影响: 未 ACK 的 Slack 会重发 (msg_id 去重), 撕裂的
#     append 由 _quarantine_torn_tail 隔离。
_shutdown = False


def _handle_sigterm(signum, frame):
    global _shutdown
    # 只做布尔赋值: logging/锁/IO 在信号 handler 里不安全, 见上。
    # 退出日志由主循环检测到 _shutdown 后打印。
    _shutdown = True


def _handle_sigint(signum, frame):
    global _shutdown
    _shutdown = True


def _wait_interruptible(secs):
    """可被停机信号唤醒的等待。

    1 秒粒度轮询 _shutdown; 信号到达后最多 1 秒返回。
    不用 threading.Event: handler 里不碰锁, 杜绝重入/死锁可能。
    用 monotonic 时钟, 不受 NTP 跳变影响。
    """
    deadline = time.monotonic() + secs
    while not _shutdown:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


class _ShutdownRequested(Exception):
    """停机信号在阻塞的连接/重试中被请求。

    信号 handler 只置 _shutdown 标志, 但主线程可能正阻塞在 SDK 内部
    (限流重试等待 / connect 握手)。这些位置检测到 _shutdown 后抛此
    异常, 把控制权交回监督循环干净退出, 而不是默默继续重试。
    """


def _issue_new_wss_url_bounded(smc):
    """可被停机信号中断的 WSS URL 签发。

    逐字对应 slack_sdk 3.45.0 BaseSocketModeClient.issue_new_wss_url 的
    ratelimited 分支, 但把"不可中断的 time.sleep(delay)+递归重试"换成:
    Retry-After 以 1 秒粒度等待 (_wait_interruptible), 期间 _shutdown
    置位则抛 _ShutdownRequested —— 停机后不再继续 apps.connections.open
    调用。挂到 client 实例上 (见 run_client_once), 初始 connect() 与
    SDK 后台重连线程 (monitor -> connect_to_new_endpoint) 都走它。
    """
    from slack_sdk.errors import SlackApiError
    while True:
        try:
            response = smc.web_client.apps_connections_open(
                app_token=smc.app_token)
            return response["url"]
        except SlackApiError as e:
            resp = getattr(e, "response", None) or {}
            if (resp.get("error") == "ratelimited") and not _shutdown:
                headers = getattr(resp, "headers", {}) or {}
                delay = int(headers.get("Retry-After", "30"))
                logging.info("Rate limited. Retrying in %d seconds...", delay)
                _wait_interruptible(delay)
                if _shutdown:
                    raise _ShutdownRequested()
                continue
            raise


def _connect_bounded(smc):
    """带停机界的 smc.connect()。

    connect() 内部可能阻塞在 socket 握手等 SDK 调用里, 那里读不到主线
    程的 _shutdown 标志。放到 daemon 线程里跑, 主线程以 1 秒粒度 join
    等待; _shutdown 置位则不再等待、抛 _ShutdownRequested, 由
    run_client_once 的 finally 做 close() 释放。daemon 线程不阻止进程
    退出; 它若事后成功/失败, 落在已关闭的 client 上, 无影响。
    connect() 本身的异常原样抛回调用方 (触发监督重建)。
    """
    outcome = {}

    def _do():
        try:
            smc.connect()
        except BaseException as e:  # noqa: BLE001 -- 转交主线程原样抛出
            outcome["error"] = e
        else:
            outcome["ok"] = True

    t = threading.Thread(target=_do, name="smc-connect", daemon=True)
    t.start()
    while t.is_alive():
        if _shutdown:
            raise _ShutdownRequested()
        t.join(timeout=1.0)
    if "error" in outcome:
        raise outcome["error"]


def _close_bounded(smc, timeout=15):
    """带停机界的 smc.close()。

    SDK 3.45.0 的 close() 会无超时 join 后台线程
    (IntervalRunner.shutdown / ThreadPoolExecutor.shutdown), 若某个
    worker 卡住则 close() 本身卡死。放到 daemon 线程里跑, 最多等
    `timeout` 秒。返回 True 表示 close() 完成; 超时返回 False 并记
    error 日志 (进程仍会退出, 不无限傻等)。
    """
    done = threading.Event()

    def _do():
        try:
            smc.close()
        except Exception:
            logging.exception("smc.close() raised")
        finally:
            done.set()

    t = threading.Thread(target=_do, name="smc-close", daemon=True)
    t.start()
    ok = done.wait(timeout)
    if not ok:
        logging.error("smc.close() did not finish within %ss; giving up",
                      timeout)
    return ok


# ---- 进程级最终停机上限 ----
# _close_bounded 只限制主线程等待 close() 的时长。若 SDK ThreadPoolExecutor
# 里有卡住的任务, Python 退出钩子仍
# 会等它, 进程退不掉 —— 2026-10-05 实测: SIGTERM 后 helper 15s 返回 False、
# 监督循环返回, 但 18s 时进程仍活着。看门狗在停机请求到达后计时, 到期
# 仍未退出则 os._exit(1) 硬结束。这是实际进程结束, 不是"打印了退出"、
# 也不是再套一层 daemon 包装。
# 恢复语义: 卡住的 handler 若已落盘但未 ACK, Slack 会重发, msg_id 去重保证
# 不重复入队; 若正好死在 append 中间, 下次启动 _quarantine_torn_tail 会隔离
# 撕裂行 (见 inbox_store)。已持久化记录可恢复; 未确认事件仍需 Slack 重投。
CLOSE_TIMEOUT_SECS = 15
FINAL_DEADLINE_SECS = 30

_shutdown_decided = threading.Event()
_watchdog_epoch = 0


def _start_final_deadline_watchdog(deadline_secs=None):
    """启动最终停机看门狗 (daemon 线程)。

    观察到停机请求或 _shutdown_decided 后开始计时; deadline_secs 秒后
    进程仍活着, 说明退出未完成 (例如 executor 任务卡住), 则
    os._exit(1) 硬结束。epoch 防止旧看门狗误杀新一轮监督循环
    (重复启动看门狗时)。
    """
    if deadline_secs is None:
        deadline_secs = FINAL_DEADLINE_SECS
    global _watchdog_epoch
    _watchdog_epoch += 1
    epoch = _watchdog_epoch

    def _watch():
        # 不等待 close() 或退出日志: 它们本身也可能被 I/O 卡住。
        while not (_shutdown or _shutdown_decided.is_set()):
            if epoch != _watchdog_epoch:
                return
            time.sleep(1)
        deadline = time.monotonic() + deadline_secs
        while time.monotonic() < deadline:
            if epoch != _watchdog_epoch:
                return
            time.sleep(1)
        if epoch != _watchdog_epoch:
            return
        # 硬停机不能依赖任何输出: 满管道或被占用的日志锁都会阻塞 print/log。
        os._exit(1)

    threading.Thread(target=_watch, name="final-deadline",
                     daemon=True).start()


def run_client_once(bot_token, app_token):
    """一次 SocketModeClient 生命周期。阻塞直到 client 失败或收到停机信号。

    正常情况只在 _shutdown 置位时返回; 其它任何退出 (异常/意外返回)
    都由 serve_forever() 重建 client。事件处理逻辑 (handle/process_event)
    原样不动。
    """
    from slack_sdk.web import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    web = WebClient(token=bot_token, **_proxy_kw(), ssl=_ssl_ctx())
    me = _ENV.get("SLACK_BOT_USER_ID") or web.auth_test().get("user_id")
    my_bot_id = _ENV.get("SLACK_BOT_ID") or None  # B 开头，thread 父消息作者比对用
    logging.info("bridge starting, bot user %s (whitelist_mode=%s)",
                 me, WHITELIST_MODE)

    def handle(client: SocketModeClient, req: SocketModeRequest):
        try:
            event = (req.payload or {}).get("event", {})
            action, payload = process_event(event, me)
            if action == "queue":
                # 1) 先可靠落盘。失败则不 ACK，Slack 会重发，
                #    msg_id 去重保证 at-least-once 不重复入队。
                saved = inbox_store.append_record(payload)
                logging.info("queued %s %s (duplicate=%s)",
                             payload["kind"], payload["msg_id"], not saved)
            elif action == "check_thread_parent":
                # 人类在频道 thread 里的回复：查父消息 -> 落盘 -> 最后 ACK。
                # 任何一步失败都不 ACK，靠 Slack 重发重试（幂等，不重复入队）。
                outcome, rec = resolve_thread_reply(
                    event, me, my_bot_id, web, inbox_store.append_record)
                if outcome == "retry":
                    logging.warning(
                        "thread reply %s:%s undecided, withholding ACK",
                        event.get("channel"), event.get("ts"))
                    return  # 不 ACK -> Slack 重发
                if outcome == "queued":
                    logging.info("queued thread_reply %s", rec["msg_id"])
                else:
                    logging.info(
                        "thread reply not under own message, drop %s:%s",
                        event.get("channel"), event.get("ts"))
                # 落到后面的公共 ACK
            else:
                logging.debug("ack_only: %s", payload)
        except Exception:
            logging.exception("persist failed; withholding ACK for redelivery")
            return  # no ACK -> Slack redelivers
        # 2) 落盘成功后再 ACK（本地写盘毫秒级，远快于 Slack 的 ~3s ACK 时限）。
        try:
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logging.exception("ack send failed")

    # 注意: SocketModeClient 构造即启动后台线程 (IntervalRunner +
    # ThreadPoolExecutor, 见 slack_sdk 3.45.0 builtin/client.py __init__)。
    # connect() 失败也必须 close(), 否则失败的 client 在进程里越积越多。
    smc = SocketModeClient(app_token=app_token, web_client=web, **_proxy_kw())
    # 实例补丁: 把 SDK 原生的递归重试 (apps.connections.open 返回
    # ratelimited 时 time.sleep(Retry-After) 后递归, 信号来了也停不下来)
    # 换成可中断的 _issue_new_wss_url_bounded。初始 connect() 与 SDK 后台
    # 重连线程都经 self.issue_new_wss_url() 调用, 补丁对两者都生效。
    smc.issue_new_wss_url = lambda: _issue_new_wss_url_bounded(smc)  # noqa: E731
    try:
        smc.socket_mode_request_listeners.append(handle)
        _connect_bounded(smc)
        logging.info("socket mode connected, listening")
        while not _shutdown:
            time.sleep(1)
    finally:
        _close_bounded(smc, CLOSE_TIMEOUT_SECS)


def serve_forever(bot_token, app_token, runner=None,
                  initial_backoff=5, max_backoff=300,
                  wait_fn=None):
    """监督循环: runner 抛出的任何异常都会触发 client 重建 + 指数退避。

    退避等待是可中断的 (1 秒粒度轮询 _shutdown): SIGTERM/SIGINT 到达后
    最多 1 秒即干净退出, 不再重建 client, 不抛 traceback。
    runner 缺省为一次完整的 client 生命周期。只有 _shutdown
    (停机信号) 会干净退出; 配置缺失等硬错误由 main() 在循环外直接
    退出, 不进重试。
    进程级最终停机上限: 由 main() 启动最终停机看门狗; 监督循环结束
    或收到停机请求后 FINAL_DEADLINE_SECS 秒内进程仍未退出
    (如 SDK executor 里有卡住的非 daemon 任务), 看门狗 os._exit(1)
    硬结束。见 _start_final_deadline_watchdog。
    """
    global _shutdown
    _shutdown_decided.clear()
    for sig, handler in ((signal.SIGTERM, _handle_sigterm),
                         (signal.SIGINT, _handle_sigint)):
        try:
            signal.signal(sig, handler)
        except ValueError:
            pass  # 非主线程 (如单测) 时跳过信号安装
    if runner is None:
        runner = lambda: run_client_once(bot_token, app_token)  # noqa: E731
    if wait_fn is None:
        wait_fn = _wait_interruptible
    backoff = initial_backoff
    while not _shutdown:
        started = time.monotonic()
        try:
            runner()
        except _ShutdownRequested:
            # 连接/重试/关闭阶段被停机信号打断: _shutdown 已置位, 不重建,
            # 直接退出。各 bounded 阶段保证信号后 1 秒粒度内抛到这里。
            logging.info("shutdown during connect/retry; exiting")
            break
        except KeyboardInterrupt:
            # 信号已由 handler 转为 _shutdown; 这里是直接 raise 时的兜底
            logging.info("keyboard interrupt, shutting down")
            _shutdown = True
            break
        except SystemExit as e:
            logging.warning("client raised SystemExit(code=%s); rebuilding",
                            e.code)
        except Exception:
            logging.exception("socket client failed; rebuilding")
        if _shutdown:
            break
        # 存活足够久说明是偶发抖动, 重置退避立即重建;
        # 连续速死则退避拉满, 防止热循环打爆日志和 API。
        if time.monotonic() - started > 300:
            backoff = initial_backoff
        else:
            wait_fn(backoff)  # 停机信号最多 1 秒内唤醒, 不再傻等满 300 秒
            if _shutdown:
                break
            backoff = min(backoff * 2, max_backoff)
    _shutdown_decided.set()
    if _shutdown:
        logging.info("shutdown requested, exiting cleanly")
    logging.info("bridge supervisor exiting")


def main():
    bot_token = _ENV.get("SLACK_BOT_TOKEN")
    app_token = _ENV.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        logging.error("missing SLACK_BOT_TOKEN / SLACK_APP_TOKEN in .env")
        sys.exit(1)
    # 进程级最终停机上限: 请求停机后, 若 stuck 的非 daemon 线程
    # 拖住 Python 退出, 看门狗在 FINAL_DEADLINE_SECS 秒后 os._exit(1)。
    _start_final_deadline_watchdog()
    serve_forever(bot_token, app_token)


if __name__ == "__main__":
    main()
