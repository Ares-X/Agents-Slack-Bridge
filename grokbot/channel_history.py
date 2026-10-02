"""Print recent messages of a channel as compact JSON lines (chronological).

Usage: python channel_history.py <channel> [limit]
给回复生成提供上下文用。失败时输出 {"error": ...}，调用方应如实报告失败、不编造。
"""
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


def main():
    if len(sys.argv) < 2:
        print("usage: channel_history.py <channel> [limit]", file=sys.stderr)
        sys.exit(1)
    channel = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    env = load_env(os.path.join(BASE, ".env")) if os.path.exists(
        os.path.join(BASE, ".env")) else {}
    proxy = env.get("PROXY_URL") or None
    ca = env.get("CA_BUNDLE") or None

    from slack_sdk.web import WebClient
    ctx = ssl.create_default_context(
        cafile=ca if ca and os.path.exists(ca) else None)
    c = WebClient(token=env["SLACK_BOT_TOKEN"],
                  **({"proxy": proxy} if proxy else {}), ssl=ctx)

    msgs = None
    for _ in range(5):
        try:
            msgs = c.conversations_history(channel=channel, limit=limit)["messages"]
            break
        except Exception:
            time.sleep(3)
    if msgs is None:
        print(json.dumps({"error": "history fetch failed"}))
        return

    names = {}

    def nm(uid):
        if uid not in names:
            try:
                p = c.users_info(user=uid)["user"].get("profile", {})
                names[uid] = p.get("display_name") or p.get("real_name") or uid
            except Exception:
                names[uid] = uid
        return names[uid]

    for m in reversed(msgs):  # chronological order
        print(json.dumps({
            "user_name": nm(m.get("user", "")),
            "text": (m.get("text") or "")[:500],
            "ts": m.get("ts", ""),
            "is_bot": bool(m.get("bot_id")),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
