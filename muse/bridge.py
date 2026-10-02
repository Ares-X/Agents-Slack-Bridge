"""Slack Socket Mode -> local inbox queue. 只收不发。

监听 bot 的私信（message.im）与频道 @mention（app_mention），
把每条消息以 JSON 行追加到 inbox.jsonl，供消费层（cron/轮询脚本/agent）
读取处理。发送请用 send.py。

配置：同目录 .env（0600），见 .env.example。
依赖：pip install slack_sdk
"""
import json
import logging
import os
import ssl
import sys
import time
import fcntl

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")
INBOX_PATH = os.path.join(BASE, "inbox.jsonl")
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

def _parse_csv_set(raw):
    """Comma-separated IDs → set of non-empty stripped strings."""
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


# 白名单：多 agent 协作时放行对端 bot 的 @/发言。
# 默认空 = 丢弃全部 bot 消息（muse 安全默认；只防自循环不够，还防陌生 bot）。
# 协作时在 .env 填 ALLOWED_BOT_USERS / ALLOWED_BOT_IDS（逗号分隔 U…/B…），二者满足其一即放行。
# 自己的消息永远被过滤，不会自循环。
ALLOWED_BOT_USERS = _parse_csv_set(_ENV.get("ALLOWED_BOT_USERS", ""))
ALLOWED_BOT_IDS = _parse_csv_set(_ENV.get("ALLOWED_BOT_IDS", ""))

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
    """Append one record with an exclusive lock. Skip duplicates."""
    key = (record["channel"], record["ts"])
    with open(INBOX_PATH, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            for line in f:
                try:
                    r = json.loads(line)
                    if (r.get("channel"), r.get("ts")) == key:
                        return False  # duplicate
                except Exception:
                    continue
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
    me = web.auth_test().get("user_id")
    logging.info("bridge starting, bot user %s", me)

    names = {}

    def disp_name(kind, id_):
        if id_ in names:
            return names[id_]
        try:
            if kind == "channel":
                r = web.conversations_info(channel=id_)
                n = r["channel"].get("name") or r["channel"].get("user") or id_
            else:
                r = web.users_info(user=id_)
                p = r["user"].get("profile", {})
                n = p.get("display_name") or p.get("real_name") or id_
            names[id_] = n
            return n
        except Exception:
            return id_

    def handle(client: SocketModeClient, req: SocketModeRequest):
        try:
            # 先 ack，Slack 才不会重发
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id))
            event = (req.payload or {}).get("event", {})

            kind = None
            if req.type == "events_api" and event.get("type") == "app_mention":
                kind = "mention"
            elif (req.type == "events_api" and event.get("type") == "message"
                    and event.get("channel_type") == "im"):
                kind = "dm"
            if not kind:
                return

            # 自己的消息永远丢弃；.env 白名单 bot 放行（协作）；其余 bot 丢弃（muse 默认）
            if event.get("user") == me:
                return
            is_bot_msg = bool(event.get("bot_id")) or \
                event.get("subtype") == "bot_message"
            if is_bot_msg and event.get("user") not in ALLOWED_BOT_USERS \
                    and event.get("bot_id") not in ALLOWED_BOT_IDS:
                return

            record = {
                "channel": event.get("channel", ""),
                "channel_name": disp_name("channel", event.get("channel", "")),
                "user": event.get("user", ""),
                "user_name": disp_name("user", event.get("user", "")),
                "text": event.get("text", ""),
                "kind": kind,                            # dm | mention
                "ts": event.get("ts", ""),
                "thread_ts": event.get("thread_ts", ""),  # 原帖回复用
                "received_at": time.time(),
                "delivered": False,
            }
            if append_inbox(record):
                logging.info("queued %s from %s in %s",
                             kind, record["user_name"], record["channel_name"])
        except Exception:
            logging.exception("handler error")

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
