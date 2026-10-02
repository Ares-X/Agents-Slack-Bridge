"""Print recent messages of a channel as compact JSON lines (chronological).

Usage: python channel_history.py <channel> [limit]
给回复生成提供上下文用。

失败语义（调用方必须处理，不可当空历史）：
  - 打印 {"error": "history fetch failed", "reason": "<异常类型: 信息>"} 到 stdout
  - 退出码 2
  调用方应保留 reason，执行可见的降级/延迟策略（见 consumer）。
"""
import json
import os
import hashlib
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
    last_err = None
    for _ in range(3):
        try:
            msgs = c.conversations_history(channel=channel, limit=limit)["messages"]
            break
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            time.sleep(3)
    if msgs is None:
        print(json.dumps({"error": "history fetch failed",
                          "reason": last_err or "unknown"}))
        sys.exit(2)

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
        full_text = m.get("text") or ""
        print(json.dumps({
            "user_name": nm(m.get("user", "")),
            "user": m.get("user", ""),
            "bot_id": m.get("bot_id", ""),
            "text": full_text[:500],
            # 全文哈希：发送核验用精确匹配，不再用 60 字前缀猜测。
            "text_sha256": hashlib.sha256(
                full_text.encode("utf-8")).hexdigest(),
            "ts": m.get("ts", ""),
            "thread_ts": m.get("thread_ts", ""),
            "is_bot": bool(m.get("bot_id")),
            # 本次发送尝试的关联证据：发送时随 POST 提交的唯一 id，
            # Slack 会原样存进消息并在 history 里回显。核验"这次发送"
            # 是否成功时必须命中它；同身份+同正文+时间窗口不够。
            "client_msg_id": m.get("client_msg_id", ""),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
