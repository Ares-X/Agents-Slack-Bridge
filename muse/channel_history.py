"""Print complete Slack messages as chronological JSON lines.

Usage: channel_history.py <channel> [limit] [--thread-ts TS|--thread TS]
                          [--all] [--cursor CURSOR] [--oldest TS] [--latest TS]
                          [--include-all-metadata]
Without --all, limit is the maximum message count (default 10). With --all,
it is the page size. An unfinished cursor is reported on stderr; stdout
contains only messages. Any page failure prints one error and exits 2,
never a partial or empty successful history.
"""
import argparse
import hashlib
import json
import os
import ssl
import sys
import time
from decimal import Decimal

BASE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    d = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def fetch_messages(client, channel, limit=10, thread_ts="", all_pages=False,
                   cursor="", oldest="", latest="", include_all_metadata=False):
    """Collect before printing so a later failure cannot look complete."""
    messages, seen_messages, seen_cursors = [], set(), set()
    while True:
        if cursor in seen_cursors:
            raise ValueError("history pagination repeated a cursor")
        seen_cursors.add(cursor)
        kwargs = {"channel": channel, "limit": min(limit, 100)}
        if oldest:
            kwargs["oldest"] = oldest
            kwargs["inclusive"] = True
        if latest:
            kwargs["latest"] = latest
        if include_all_metadata:
            kwargs["include_all_metadata"] = True
        if not all_pages:
            kwargs["limit"] = min(limit - len(messages), 100)
        if cursor:
            kwargs["cursor"] = cursor
        method = client.conversations_history
        if thread_ts:
            method = client.conversations_replies
            kwargs["ts"] = thread_ts
        response = None
        for attempt in range(3):
            try:
                response = method(**kwargs)
                if response.get("ok") is False:
                    raise RuntimeError(response.get("error") or "Slack history rejected")
                if not isinstance(response.get("messages"), list):
                    raise ValueError("history response missing messages list")
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(3)
        for message in response["messages"]:
            if not isinstance(message, dict) or not message.get("ts"):
                raise ValueError("history message missing timestamp")
            key = message["ts"]
            if key not in seen_messages:
                seen_messages.add(key)
                messages.append(message)
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
        if response.get("has_more") and not cursor:
            raise ValueError("history has_more without next_cursor")
        if not cursor or (not all_pages and len(messages) >= limit):
            break
    messages.sort(key=lambda m: Decimal(m["ts"]))
    return messages, cursor


def message_record(client, message, channel, names):
    uid = message.get("user", "")
    if uid and uid not in names:
        try:
            profile = client.users_info(user=uid)["user"].get("profile", {})
            names[uid] = profile.get("display_name") or profile.get("real_name") or uid
        except Exception:
            names[uid] = uid
    text = message.get("text") or ""
    record = {"channel": channel, "user_name": names.get(uid, uid), "user": uid,
              "bot_id": message.get("bot_id", ""), "text": text,
              "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
              "ts": message["ts"], "thread_ts": message.get("thread_ts", ""),
              "is_bot": bool(message.get("bot_id")),
              "client_msg_id": message.get("client_msg_id", "")}
    # Structured approval/control cards and edited text remain source evidence.
    for field in ("subtype", "blocks", "attachments", "edited", "bot_profile",
                  "app_id", "username", "reply_count", "latest_reply", "metadata"):
        if field in message:
            record[field] = message[field]
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel")
    parser.add_argument("limit", nargs="?", type=int, default=10)
    parser.add_argument("--thread-ts", "--thread", dest="thread_ts", default="")
    parser.add_argument("--all", dest="all_pages", action="store_true")
    parser.add_argument("--cursor", default="")
    parser.add_argument("--oldest", default="")
    parser.add_argument("--latest", default="")
    parser.add_argument("--include-all-metadata", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("limit must be positive")
    try:
        env = load_env(os.path.join(BASE, ".env")) if os.path.exists(
            os.path.join(BASE, ".env")) else {}
        from net_config import read_proxy_config
        from slack_sdk.web import WebClient
        proxy, ca = read_proxy_config(env)
        ctx = ssl.create_default_context(cafile=ca if ca and os.path.exists(ca) else None)
        client = WebClient(token=env.get("SLACK_BOT_TOKEN") or os.environ["SLACK_BOT_TOKEN"],
                           **({"proxy": proxy} if proxy else {}), ssl=ctx)
        messages, cursor = fetch_messages(client, args.channel, args.limit,
                                          args.thread_ts, args.all_pages, args.cursor,
                                          args.oldest, args.latest, args.include_all_metadata)
        names = {}
        records = [message_record(client, m, args.channel, names) for m in messages]
    except Exception as exc:
        print(json.dumps({"error": "history fetch failed",
                          "reason": "%s: %s" % (type(exc).__name__, exc)},
                         ensure_ascii=False))
        return 2
    for record in records:
        print(json.dumps(record, ensure_ascii=False))
    if cursor:
        print(json.dumps({"next_cursor": cursor}, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
