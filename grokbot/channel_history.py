"""Print recent messages of a channel as compact JSON lines (chronological).

Usage: python channel_history.py <channel> [limit] [--thread-ts TS] [--all]
--thread is an alias. limit is the page size; --all follows cursors. Without
--all, a pagination JSON row exposes any remaining cursor. Bodies are complete.
给回复生成提供上下文用。失败时输出 {"error": ...} 并 exit 1；
调用方应如实报告失败、不编造、不假装已读上下文。
"""
import argparse
import json
import os
import ssl
import sys
import time

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


def read_messages(client, channel, limit, *, thread_ts=None, cursor=None, all_pages=False):
    """Read channel or thread context; expose incomplete pages to the caller."""
    messages = []
    seen_cursors = set()
    while True:
        if cursor:
            if cursor in seen_cursors:
                raise ValueError("history pagination repeated a cursor")
            seen_cursors.add(cursor)
        params = {"channel": channel, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if thread_ts:
            params["ts"] = thread_ts
        method = client.conversations_replies if thread_ts else client.conversations_history
        for attempt in range(5):
            try:
                page = method(**params)
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(3)
        messages.extend(page["messages"])
        cursor = (page.get("response_metadata") or {}).get("next_cursor", "").strip()
        has_more = bool(page.get("has_more") or cursor)
        if has_more and not cursor:
            raise ValueError("history has more messages but no continuation cursor")
        if not all_pages or not has_more:
            break
    # Slack returns channel history newest-first and replies oldest-first.
    # Dedup only identical Slack message identities at pagination boundaries.
    ordered = messages if thread_ts else reversed(messages)
    rows = []
    seen_ts = set()
    for message in ordered:
        ts = message.get("ts")
        if ts and ts in seen_ts:
            continue
        if ts:
            seen_ts.add(ts)
        rows.append(message)
    return rows, cursor


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read Slack channel/thread context as JSONL")
    ap.add_argument("channel")
    ap.add_argument("limit", nargs="?", type=int, default=10, help="page size (1..200)")
    ap.add_argument("--thread-ts", "--thread", dest="thread_ts")
    ap.add_argument("--all", action="store_true", help="follow all continuation cursors")
    ap.add_argument("--cursor", help="resume a previous page")
    args = ap.parse_args(argv)
    if not 1 <= args.limit <= 200:
        ap.error("limit must be between 1 and 200")

    env = load_env(os.path.join(BASE, ".env")) if os.path.exists(
        os.path.join(BASE, ".env")) else {}
    proxy = env.get("PROXY_URL") or None
    ca = env.get("CA_BUNDLE") or None

    if not env.get("SLACK_BOT_TOKEN"):
        print(json.dumps({"error": "missing SLACK_BOT_TOKEN"}))
        return 1

    from slack_sdk.web import WebClient
    ctx = ssl.create_default_context(
        cafile=ca if ca and os.path.exists(ca) else None)
    c = WebClient(token=env["SLACK_BOT_TOKEN"],
                  **({"proxy": proxy} if proxy else {}), ssl=ctx)

    try:
        msgs, cursor = read_messages(c, args.channel, args.limit,
                                    thread_ts=args.thread_ts, cursor=args.cursor,
                                    all_pages=args.all)
    except Exception as exc:
        print(json.dumps({
            "error": f"history fetch failed: {exc!r}",
        }, ensure_ascii=False))
        return 1

    names = {}

    def nm(uid):
        if uid not in names:
            try:
                p = c.users_info(user=uid)["user"].get("profile", {})
                names[uid] = p.get("display_name") or p.get("real_name") or uid
            except Exception:
                names[uid] = uid
        return names[uid]

    for m in msgs:  # chronological order
        print(json.dumps({
            "user_name": nm(m.get("user", "")),
            "user": m.get("user", ""),
            "text": m.get("text") or "",
            "ts": m.get("ts", ""),
            "thread_ts": m.get("thread_ts") or args.thread_ts or "",
            "is_bot": bool(m.get("bot_id")),
        }, ensure_ascii=False))
    if cursor:
        print(json.dumps({"pagination": {"channel": args.channel,
                                         "thread_ts": args.thread_ts,
                                         "next_cursor": cursor,
                                         "complete": False}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
