"""Slack Socket Mode -> local inbox queue. 只收不发。

监听：
  - 私信（message.im）
  - 频道 @mention（app_mention）
  - 本 bot 消息线程下的跟帖回复（message.channels / message.groups + thread_ts，
    且父消息 user==me；无需再 @mention）

把每条消息以 JSON 行追加到 inbox.jsonl，供消费层（cron/轮询脚本/agent）
读取处理。发送请用 send.py。

可靠性顺序：先 durable 入队（flock + fsync），再 Socket Mode ACK。
入队路径不做频道/用户名查名（channel_name/user_name 先留空或用 id）；
若入队失败则不 ACK，让 Slack 重试；重复入队由 (channel, ts) 去重消化。

线程父消息判定：
  1) bot_sent_ts.json 缓存命中（send.py 成功后写入）→ 接受
  2) 否则 conversations.replies 查父消息 user==me → 接受并写入缓存
  父查失败 → 不 ACK（Slack 重试）；确认非本 bot → ACK 丢弃

Grok Bot 多 agent 频道协作默认：
  - 永远丢弃自己的 user_id（auth_test），防止自循环
  - 默认允许其他 bot 的 app_mention / 发言进入队列（不要改成丢弃全部 bot）
    （Grok Bot 与其他 agent 常在同一频道互相 @）
  - 若 .env 设置了 ALLOWED_BOT_USERS / ALLOWED_BOT_IDS（逗号分隔），
    则改为白名单模式：仅这些 bot 放行，其他 bot 丢弃（可选收紧）
  - DM / channel 的 edit/delete/bot_message 等 subtype 显式过滤
  - 消费层：REPLY_IN_THREAD=0 时 mention/dm 仍顶层；kind=thread_reply
    强制跟帖（同 thread_ts），见 AGENT_WAKE.md / .env.example

配置：同目录 .env（0600），见 .env.example。
依赖：pip install slack_sdk

Slack App：改 manifest 后须在 api.slack.com 重新 Apply Manifest 并重装/
更新事件订阅（至少增加 message.channels；私频加 message.groups）。
"""
import logging
import os
import ssl
import sys
import time

from bot_ts_cache import contains as cache_contains
from bot_ts_cache import remember as cache_remember
from inbox_store import append_record

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")
INBOX_PATH = os.path.join(BASE, "inbox.jsonl")
LOG_PATH = os.path.join(BASE, "bridge.log")

# Subtypes that are not actionable user messages (empty/misleading user+text).
DROP_SUBTYPES = frozenset({
    "message_changed",
    "message_deleted",
    "message_replied",
    "tombstone",
    "bot_message",  # avoid bot echo / loop via subtype path
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "group_topic",
    "group_purpose",
    "group_name",
    "group_archive",
    "group_unarchive",
    "bot_add",
    "bot_remove",
    "pinned_item",
    "unpinned_item",
    "ekm_access_denied",
})

# Back-compat alias used by older tests
DM_DROP_SUBTYPES = DROP_SUBTYPES


def load_env(path):
    d = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def _parse_csv_set(raw):
    """Comma-separated IDs → set of non-empty stripped strings."""
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


_ENV = load_env(ENV_PATH) if os.path.exists(ENV_PATH) else {}
PROXY_URL = _ENV.get("PROXY_URL") or None      # 出站代理，直连留空
CA_BUNDLE = _ENV.get("CA_BUNDLE") or None      # 自签 CA 路径，默认系统 CA 留空

# Optional whitelist from .env. If BOTH empty → allow any bot except self.
# If either set → whitelist mode (must match user ID or bot ID).
ALLOWED_BOT_USERS = _parse_csv_set(_ENV.get("ALLOWED_BOT_USERS", ""))
ALLOWED_BOT_IDS = _parse_csv_set(_ENV.get("ALLOWED_BOT_IDS", ""))
WHITELIST_MODE = bool(ALLOWED_BOT_USERS or ALLOWED_BOT_IDS)

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


def append_inbox(record):
    """Append one record durably. Skip duplicates on (channel, ts)."""
    return append_record(INBOX_PATH, record)


def should_drop_dm_event(event):
    """True if this IM event should not become an inbox task."""
    return should_drop_message_event(event)


def should_drop_message_event(event):
    """True if this message event should not become an inbox task."""
    subtype = event.get("subtype") or ""
    if subtype in DROP_SUBTYPES:
        return True
    # Edits/deletes sometimes omit top-level user; never queue those.
    if not event.get("user") and not event.get("bot_id"):
        return True
    return False


def classify_inbound_kind(event, req_type):
    """Return 'mention' | 'dm' | 'thread_reply' | None.

    'thread_reply' here means a *candidate* (channel/group message with
    thread_ts). Caller must still verify parent is our bot.
    """
    if req_type != "events_api":
        return None
    et = event.get("type")
    if et == "app_mention":
        return "mention"
    if et != "message":
        return None
    ct = event.get("channel_type") or ""
    if ct == "im":
        return "dm"
    # Public / private channel messages: only thread replies are candidates.
    if ct in ("channel", "group"):
        thread_ts = (event.get("thread_ts") or "").strip()
        ts = (event.get("ts") or "").strip()
        # Real reply: thread_ts present and (usually) differs from ts.
        if thread_ts and thread_ts != ts:
            return "thread_reply"
    return None


def parent_message_is_me(parent, me):
    """True if conversations.replies parent belongs to our bot user."""
    if not parent or not me:
        return False
    return parent.get("user") == me


def fetch_thread_parent(web, channel, thread_ts):
    """Return parent message dict or None. Raises on transport/API failure."""
    r = web.conversations_replies(
        channel=channel, ts=thread_ts, limit=1, inclusive=True
    )
    msgs = r.get("messages") or []
    return msgs[0] if msgs else None


def is_our_thread_parent(web, channel, thread_ts, me, *, cache_path=None):
    """True if thread parent is our bot. Uses cache then API.

    Returns (ok: bool, error: Optional[BaseException]).
    ok=False + error set → caller should NOT ack (retry).
    ok=False + error None → confirmed not ours → ack drop.
    """
    from bot_ts_cache import DEFAULT_PATH
    path = cache_path or DEFAULT_PATH
    if cache_contains(channel, thread_ts, path=path):
        return True, None
    try:
        parent = fetch_thread_parent(web, channel, thread_ts)
    except Exception as e:
        return False, e
    if parent_message_is_me(parent, me):
        cache_remember(channel, thread_ts, path=path)
        return True, None
    return False, None


def should_reply_in_thread(message, reply_in_thread_env=False):
    """Env REPLY_IN_THREAD for mentions/dms; always True for thread_reply."""
    if (message or {}).get("kind") == "thread_reply":
        return True
    return bool(reply_in_thread_env)


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
    # Prefer explicit SLACK_BOT_USER_ID; else auth_test
    me = (_ENV.get("SLACK_BOT_USER_ID") or "").strip() or web.auth_test().get("user_id")
    logging.info(
        "bridge starting, bot user %s (whitelist_mode=%s)", me, WHITELIST_MODE
    )

    # Name lookup is intentionally NOT on the receive→ack path (network I/O
    # would delay durable enqueue + Socket Mode ACK). Enrich offline if needed.
    # Exception: thread_reply parent check (cache-first; API only on miss).

    def ack(client: SocketModeClient, req: SocketModeRequest):
        client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id))

    def handle(client: SocketModeClient, req: SocketModeRequest):
        # Durable enqueue FIRST, then fast Socket Mode ACK.
        # On failure before durable write: do NOT ack → Slack retries.
        try:
            event = (req.payload or {}).get("event", {})

            kind = classify_inbound_kind(event, req.type)
            if not kind:
                # Non-target events: ack so Slack stops delivering them.
                ack(client, req)
                return

            if should_drop_message_event(event):
                logging.info(
                    "drop %s subtype=%s user=%s",
                    kind, event.get("subtype"), event.get("user"),
                )
                ack(client, req)
                return

            # Always drop self (prevent self-loop)
            if event.get("user") == me:
                ack(client, req)
                return

            if kind == "thread_reply":
                ch0 = event.get("channel", "") or ""
                tts = (event.get("thread_ts") or "").strip()
                ok, err = is_our_thread_parent(web, ch0, tts, me)
                if err is not None:
                    logging.error(
                        "thread parent lookup failed (no ack): %s", err
                    )
                    return  # no ack → Slack retry
                if not ok:
                    logging.info(
                        "drop thread_reply not under us ch=%s thread_ts=%s",
                        ch0, tts,
                    )
                    ack(client, req)
                    return

            is_bot_msg = bool(event.get("bot_id")) or \
                event.get("subtype") == "bot_message"
            if is_bot_msg:
                if WHITELIST_MODE:
                    # Only allow listed bot users / bot IDs
                    if (event.get("user") not in ALLOWED_BOT_USERS
                            and event.get("bot_id") not in ALLOWED_BOT_IDS):
                        ack(client, req)
                        return
                # else: allow any other bot (multi-agent channels)

            ch = event.get("channel", "") or ""
            uid = event.get("user", "") or ""
            record = {
                "channel": ch,
                # ids first; names left empty for fast ACK (enrich later offline)
                "channel_name": "",
                "user": uid,
                "user_name": "",
                "text": event.get("text", ""),
                "kind": kind,                            # dm | mention | thread_reply
                "ts": event.get("ts", ""),
                "thread_ts": event.get("thread_ts", ""),  # 原帖回复用
                "received_at": time.time(),
                "delivered": False,
                "reply_status": None,  # None|retryable|sending|sent|uncertain
            }
            # Durable write under lock+fsync; duplicate → still ACK.
            appended = append_inbox(record)
            ack(client, req)
            if appended:
                logging.info("queued %s from %s in %s",
                             kind, uid or "?", ch or "?")
            else:
                logging.info("duplicate skip %s %s",
                             record.get("channel"), record.get("ts"))
        except Exception:
            logging.exception("handler error (no ack — Slack may retry)")

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
