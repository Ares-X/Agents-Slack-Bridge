"""Post a reply back to Slack. Token 从 .env 读（0600），绝不在 argv/日志里出现。

Usage:
  echo "正文" | python send.py <channel> [--thread-ts <ts>]
正文走 stdin，避免进 shell 历史。

Outcome lines (stdout/stderr) for consumer classification:
  sent ok: True …     — Slack accepted (ok)
  sent ok: False …    — Slack rejected with proven-not-sent error (retryable)
  not_sent: <reason>  — failed before/without accepting post (retryable)
  send_error: <…>     — ambiguous / possible partial success → uncertain

WebClient is constructed with retry_handlers=[] so a post that is accepted
then disconnects is NOT retried inside one claim (would double-post).
"""
import os
import ssl
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

# Slack API errors that PROVE the message was not posted (safe to auto-retry).
# internal_error / fatal_error / unknown codes → uncertain (may be after accept).
PROVEN_NOT_SENT_SLACK_ERRORS = frozenset({
    "channel_not_found",
    "not_in_channel",
    "is_archived",
    "channel_is_archived",
    "msg_too_long",
    "no_text",
    "invalid_auth",
    "not_authed",
    "account_inactive",
    "token_expired",
    "token_revoked",
    "missing_scope",
    "cannot_reply_to_message",
    "thread_not_found",
    "ekm_access_denied",
    "invalid_arguments",
    "invalid_charset",
    "as_user_not_supported",
    "restricted_action",
    "access_denied",
    "missing_post_type",
    "is_inactive",
    "user_not_found",
    "cant_update_message",
})


def load_env(path):
    d = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def build_web_client(token, *, proxy=None, ssl_context=None):
    """Build WebClient with retries disabled (one POST attempt per claim)."""
    from slack_sdk.web import WebClient
    kw = {
        "token": token,
        "ssl": ssl_context,
        "retry_handlers": [],  # no SDK connection/rate retries → no double POST
    }
    if proxy:
        kw["proxy"] = proxy
    return WebClient(**kw)


def classify_slack_api_error(err_code: str) -> str:
    """Return 'not_sent' or 'uncertain' for a Slack API error string."""
    code = (err_code or "").strip()
    if code in PROVEN_NOT_SENT_SLACK_ERRORS or code == "ok_false":
        return "not_sent"
    return "uncertain"


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
        from slack_sdk.errors import SlackApiError
    except ImportError as e:
        print(f"not_sent: slack_sdk import: {e}", file=sys.stderr)
        sys.exit(1)

    ctx = ssl.create_default_context(
        cafile=ca if ca and os.path.exists(ca) else None)
    c = build_web_client(env["SLACK_BOT_TOKEN"], proxy=proxy, ssl_context=ctx)
    try:
        r = c.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    except SlackApiError as e:
        err = ""
        try:
            err = e.response.get("error") if e.response is not None else str(e)
        except Exception:
            err = str(e)
        kind = classify_slack_api_error(str(err))
        if kind == "not_sent":
            print(f"not_sent: slack_api {err}", file=sys.stderr)
            print("sent ok: False")
            sys.exit(1)
        # internal_error / fatal_error / unknown — may be after partial success
        print(f"send_error: slack_api {err}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        # Timeout / connection reset / etc. — may have been accepted. Uncertain.
        print(f"send_error: {e}", file=sys.stderr)
        sys.exit(2)

    ok = bool(r.get("ok"))
    print("sent ok:", ok, "ts:", r.get("ts"))
    if not ok:
        err = str(r.get("error") or "ok_false")
        if classify_slack_api_error(err) == "uncertain":
            print(f"send_error: slack_api {err}", file=sys.stderr)
            sys.exit(2)
        print(f"not_sent: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
