"""Post a reply back to Slack. Token 从 .env 读（0600），绝不在 argv/日志里出现。

Usage:
  echo "正文" | python send.py <channel> [--thread-ts <ts>]
正文走 stdin，避免进 shell 历史。

Outcome lines (stdout/stderr) for consumer classification:
  sent ok: True …     — Slack accepted (ok)
  sent ok: False …    — Slack rejected (proven not sent → retryable)
  not_sent: <reason>  — failed before/without accepting post (retryable)
  send_error: <…>     — ambiguous (timeout/crash path) → uncertain; no not_sent
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
        print("not_sent: usage", file=sys.stderr)
        sys.exit(1)
    channel = args[0]
    thread_ts = None
    if "--thread-ts" in args:
        try:
            thread_ts = args[args.index("--thread-ts") + 1]
        except IndexError:
            print("not_sent: missing --thread-ts value", file=sys.stderr)
            sys.exit(1)
    text = sys.stdin.read()
    if not text.strip():
        print("not_sent: empty message", file=sys.stderr)
        sys.exit(1)
    if not env.get("SLACK_BOT_TOKEN"):
        print("not_sent: missing SLACK_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)

    try:
        from slack_sdk.web import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as e:
        print(f"not_sent: slack_sdk import: {e}", file=sys.stderr)
        sys.exit(1)

    ctx = ssl.create_default_context(
        cafile=ca if ca and os.path.exists(ca) else None)
    c = WebClient(token=env["SLACK_BOT_TOKEN"],
                  **({"proxy": proxy} if proxy else {}), ssl=ctx)
    try:
        r = c.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    except SlackApiError as e:
        # API responded with an error → message was not posted (proven).
        err = ""
        try:
            err = e.response.get("error") if e.response is not None else str(e)
        except Exception:
            err = str(e)
        print(f"not_sent: slack_api {err}", file=sys.stderr)
        print("sent ok: False")
        sys.exit(1)
    except Exception as e:
        # Timeout / connection reset / etc. — may have been accepted. Uncertain.
        print(f"send_error: {e}", file=sys.stderr)
        sys.exit(2)

    ok = bool(r.get("ok"))
    print("sent ok:", ok, "ts:", r.get("ts"))
    if not ok:
        print("not_sent: ok_false", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
