"""Post a reply back to Slack. Token 从 .env 读（0600），绝不在 argv/日志里出现。

Usage:
  echo "正文" | python send.py <channel> [--thread-ts <ts>] [--mention <UID>]...
正文走 stdin，避免进 shell 历史。
--mention 可重复：主动点名某人（显式 <@UID>）。默认正文里的 mention
  原样发送——调用方负责先做 strip_mentions() 脱敏，见 consumer。

退出语义（调用方判定"是否已发出"的唯一依据）：
  - stdout 含 "sent ok: True"   -> 已发出（exit 0）
  - stdout 含 "sent ok: False"  -> API 明确拒绝，证明未发送
  - stderr 含 "RESULT not-sent" -> 死在 API 调用之前，证明未发送
  - stderr 含 "RESULT uncertain"（exit 2）、超时、无特征输出 ->
    不确定：请求可能已被接受但响应丢失，调用方绝不能自动重发，
    必须走 history 核验。
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
    mentions = []
    for i, a in enumerate(args):
        if a == "--mention" and i + 1 < len(args):
            uid = args[i + 1].strip()
            if uid:
                mentions.append(uid)
    text = sys.stdin.read()
    if not text.strip():
        # 明确死在 API 调用之前：调用方可视为"未发送"，下轮重试安全。
        print("RESULT not-sent: empty message", file=sys.stderr)
        sys.exit(1)
    if mentions:
        # 显式点名追加在正文末尾；调用方已明确表达点名意图。
        text = text.rstrip() + " " + " ".join(f"<@{u}>" for u in mentions)

    from slack_sdk.web import WebClient
    ctx = ssl.create_default_context(
        cafile=ca if ca and os.path.exists(ca) else None)
    token = env.get("SLACK_BOT_TOKEN")
    if not token:
        # 明确死在 API 调用之前：调用方可视为"未发送"。
        print("RESULT not-sent: missing SLACK_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)
    c = WebClient(token=token,
                  **({"proxy": proxy} if proxy else {}), ssl=ctx,
                  # NOTE: SDK 自动重试在此刻意关闭。一次 POST 若首个响应
                  # 丢失而被 SDK 重发，会在频道里产生重复消息；这里快速
                  # 失败，由调用方经 history 核验后再决定（绝不盲目重发）。
                  retry_handlers=[])
    try:
        r = c.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    except Exception as e:
        # 请求可能已被 Slack 接受、只是响应丢失：调用方必须视为不确定，
        # 绝不能当成"未发送"去自动重发。
        print("RESULT uncertain: %s: %s" % (type(e).__name__, e),
              file=sys.stderr)
        sys.exit(2)
    print("sent ok:", r.get("ok"), "ts:", r.get("ts"))


if __name__ == "__main__":
    main()
