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
import ssl
import sys
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
PROXY_URL = _ENV.get("PROXY_URL") or None      # 出站代理，直连留空
CA_BUNDLE = _ENV.get("CA_BUNDLE") or None      # 自签 CA 路径，默认系统 CA 留空

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
            # 是不是我们自己发的，handle() 里先 ACK 再查。
            return ("check_thread_parent", event)
        return ("ack_only", "not-subscribed-or-unsupported-subtype")
    if is_own_message(event, me):
        return ("ack_only", "own-message")
    if is_bot_message(event) and not bot_allowed(event):
        return ("ack_only", "bot-not-allowlisted")
    return ("queue", build_record(event, kind))


def main():
    bot_token = _ENV.get("SLACK_BOT_TOKEN")
    app_token = _ENV.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        logging.error("missing SLACK_BOT_TOKEN / SLACK_APP_TOKEN in .env")
        sys.exit(1)

    from slack_sdk.web import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    web = WebClient(token=bot_token, **_proxy_kw(), ssl=_ssl_ctx())
    me = _ENV.get("SLACK_BOT_USER_ID") or web.auth_test().get("user_id")
    my_bot_id = _ENV.get("SLACK_BOT_ID") or None  # B 开头，thread 父消息作者比对用
    logging.info("bridge starting, bot user %s (whitelist_mode=%s)",
                 me, WHITELIST_MODE)

    def thread_parent_is_mine(channel, thread_ts):
        """查 thread 父消息的作者是不是我们自己。best-effort：查不到就当不是。"""
        try:
            resp = web.conversations_replies(channel=channel, ts=thread_ts,
                                             limit=1, inclusive=True)
            msgs = resp.get("messages") or []
            if not msgs:
                return False
            parent = msgs[0]
            if me and parent.get("user") == me:
                return True
            return bool(my_bot_id and parent.get("bot_id") == my_bot_id)
        except Exception:
            logging.exception("thread parent check failed for %s:%s",
                              channel, thread_ts)
            return False

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
                # 人类在频道 thread 里的回复：先 ACK（父消息查询是慢操作，
                # 不能占着 Slack 的 ACK 时限），再查父消息作者决定是否入队。
                try:
                    client.send_socket_mode_response(
                        SocketModeResponse(envelope_id=req.envelope_id))
                except Exception:
                    logging.exception("ack send failed")
                if thread_parent_is_mine(event.get("channel", ""),
                                         event.get("thread_ts", "")):
                    rec = build_record(event, "thread_reply")
                    saved = inbox_store.append_record(rec)
                    logging.info("queued thread_reply %s (duplicate=%s)",
                                 rec["msg_id"], not saved)
                else:
                    logging.info("thread reply not under own message, drop %s:%s",
                                 event.get("channel"), event.get("ts"))
                return  # 已 ACK，直接返回
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

    smc = SocketModeClient(app_token=app_token, web_client=web, **_proxy_kw())
    smc.socket_mode_request_listeners.append(handle)
    smc.connect()
    logging.info("socket mode connected, listening")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            smc.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
