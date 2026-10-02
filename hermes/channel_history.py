#!/usr/bin/env python3
"""读取 Slack 频道/线程历史，输出紧凑 JSON lines（按时间正序）。

零第三方依赖（stdlib urllib），供 Hermes agent 在终端里直接调用来补频道上下文。

用法:
  python3 channel_history.py <channel_id> [limit]            # 频道最近消息
  python3 channel_history.py <channel_id> --thread <ts> [limit]  # 某条消息的 thread
  python3 channel_history.py <channel_id> --resolve          # 顺带把 user ID 解析成名字

token 解析顺序: 环境变量 SLACK_BOT_TOKEN -> ~/.hermes/.env 里的 SLACK_BOT_TOKEN
失败时输出一行 {"error": ...}，调用方应如实报告、不编造。
token 永不回显、永不写日志。
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://slack.com/api/"


def load_token():
    tok = os.environ.get("SLACK_BOT_TOKEN", "")
    if tok:
        return tok.strip()
    # ~/.hermes/.env 解析（不引入 python-dotenv）
    for env_path in (os.environ.get("HERMES_ENV", ""), os.path.expanduser("~/.hermes/.env")):
        if env_path and os.path.isfile(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() == "SLACK_BOT_TOKEN":
                                return v.strip().strip('"').strip("'")
            except OSError:
                continue
    return ""


def call(method, token, **params):
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
    req = urllib.request.Request(
        API + method + "?" + qs,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        return {"error": data.get("error", "unknown_error")}
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("limit", nargs="?", type=int, default=20)
    ap.add_argument("--thread", dest="thread_ts", default=None, help="读这个 ts 的 thread")
    ap.add_argument("--resolve", action="store_true", help="解析 user/bot ID 为显示名")
    args = ap.parse_args()

    token = load_token()
    if not token or not token.startswith("xoxb-"):
        print(json.dumps({"error": "SLACK_BOT_TOKEN not found (env or ~/.hermes/.env)"}))
        sys.exit(1)

    names = {}

    def nm(uid):
        if not uid:
            return ""
        if uid not in names:
            info = call("users.info", token, user=uid)
            names[uid] = (info.get("user") or {}).get("name", uid) if "error" not in info else uid
        return names[uid]

    method, key = ("conversations.replies", "messages") if args.thread_ts else ("conversations.history", "messages")
    data = call(method, token, channel=args.channel, limit=min(args.limit, 200), ts=args.thread_ts)
    if "error" in data:
        print(json.dumps({"error": data["error"]}, ensure_ascii=False))
        sys.exit(1)

    msgs = data.get(key) or []
    out = []
    for m in msgs:
        row = {
            "ts": m.get("ts", ""),
            "user": m.get("user", "") or m.get("bot_id", ""),
            "text": (m.get("text") or "").strip(),
        }
        if m.get("thread_ts") and m["thread_ts"] != m.get("ts"):
            row["thread_ts"] = m["thread_ts"]
        if args.resolve and row["user"]:
            row["user_name"] = nm(row["user"])
        out.append(row)

    for row in reversed(out):  # 正序输出
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
