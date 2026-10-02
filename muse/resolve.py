"""Resolve Slack IDs to display names (consumer-side helper).

Usage:
  python resolve.py user <U...>      -> display name of a user/bot
  python resolve.py channel <C...|D...> -> channel name (or user id for IM)

bridge.py 热路径不做名称查询；消费层需要可读名字时调这个。
失败时原样输出 ID，不抛异常。
"""
import os
import ssl
import sys

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
    if len(sys.argv) != 3 or sys.argv[1] not in ("user", "channel"):
        print("usage: resolve.py user|channel <ID>", file=sys.stderr)
        sys.exit(2)
    kind, id_ = sys.argv[1], sys.argv[2]
    env = load_env(os.path.join(BASE, ".env")) if os.path.exists(
        os.path.join(BASE, ".env")) else {}
    proxy = env.get("PROXY_URL") or None
    ca = env.get("CA_BUNDLE") or None
    try:
        from slack_sdk.web import WebClient
        ctx = ssl.create_default_context(
            cafile=ca if ca and os.path.exists(ca) else None)
        c = WebClient(token=env["SLACK_BOT_TOKEN"],
                      **({"proxy": proxy} if proxy else {}), ssl=ctx)
        if kind == "channel":
            r = c.conversations_info(channel=id_)["channel"]
            print(r.get("name") or r.get("user") or id_)
        else:
            p = c.users_info(user=id_)["user"].get("profile", {})
            print(p.get("display_name") or p.get("real_name") or id_)
    except Exception as e:
        print(f"resolve failed ({e}); falling back to ID", file=sys.stderr)
        print(id_)


if __name__ == "__main__":
    main()
