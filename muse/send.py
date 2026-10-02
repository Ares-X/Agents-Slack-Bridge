"""Post a reply back to Slack. Token 从 .env 读（0600），绝不在 argv/日志里出现。

Usage:
  echo "正文" | python send.py <channel> [--thread-ts <ts>]
正文走 stdin，避免进 shell 历史。
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
    env = load_env(os.path.join(BASE, ".env")) if os.path.exists(
        os.path.join(BASE, ".env")) else {}
    proxy = env.get("PROXY_URL") or None
    ca = env.get("CA_BUNDLE") or None

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(1)
    channel = args[0]
    thread_ts = None
    if "--thread-ts" in args:
        thread_ts = args[args.index("--thread-ts") + 1]
    text = sys.stdin.read()
    if not text.strip():
        print("empty message", file=sys.stderr)
        sys.exit(1)

    from slack_sdk.web import WebClient
    ctx = ssl.create_default_context(
        cafile=ca if ca and os.path.exists(ca) else None)
    c = WebClient(token=env["SLACK_BOT_TOKEN"],
                  **({"proxy": proxy} if proxy else {}), ssl=ctx)
    r = c.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    print("sent ok:", r.get("ok"), "ts:", r.get("ts"))


if __name__ == "__main__":
    main()
