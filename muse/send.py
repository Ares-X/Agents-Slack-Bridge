"""Post a reply back to Slack. Token 从 .env 读（0600），绝不在 argv/日志里出现。

Usage:
  echo "正文" | python send.py <channel> [--thread-ts <ts>] [--mention <UID>]...
           [--client-msg-id <id>]
正文走 stdin，避免进 shell 历史。
--mention 可重复：主动点名某人（显式 <@UID>）。默认正文里的 mention
  原样发送——调用方负责先做 strip_mentions() 脱敏，见 consumer。
--client-msg-id：本次发送尝试的唯一关联证据，随 POST 提交；Slack 会
  把它存进消息并在 conversations.history 里回显，供调用方核验"这次
  发送"是否成功（同身份+同正文+时间窗口不能单独证明）。

退出语义（调用方判定"是否已发出"的唯一依据）：
  - stdout 含 "sent ok: True"   -> 已发出（exit 0）
  - stdout 含 "sent ok: False"  -> API 明确拒绝，证明未发送
  - stdout JSON 的 result=retry_wait（exit 75）-> 限流拒绝，未发送；
    调用方必须持久保存 retry_at，到期前不得重试
  - stderr 含 "RESULT not-sent" -> 死在 API 调用之前，证明未发送
  - stderr 含 "RESULT uncertain"（exit 2）、超时、无特征输出 ->
    不确定：请求可能已被接受但响应丢失，调用方绝不能自动重发，
    必须走 history 核验。

限流（HTTP 429 / ratelimited / rate_limited）：按 Retry-After 返回
绝对重试时间 retry_at；缺失或无效时默认等待 60 秒。本进程不等待或
重发，由 consumer 持久延期，避免重启或较短的轮询周期提前重试。
连接断开和其他未知结果仍走 uncertain，SDK 的自动重试保持关闭。
"""
import json
import math
import os
import ssl
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))


def _is_ratelimited(exc):
    """是否为 Slack 的限流拒绝（HTTP 429 或两种限流错误码）。

    鸭子类型判定：只有 SlackApiError 带有 .response；其他异常一律
    不是限流，走 uncertain。
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    if getattr(resp, "status_code", None) == 429:
        return True
    try:
        return resp.get("error") in {"ratelimited", "rate_limited"}
    except Exception:
        return False


def _retry_after_seconds(exc, default=60.0):
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    try:
        value = next((v for k, v in headers.items()
                      if k.lower() == "retry-after"), None)
        if value is None and resp is not None:
            value = resp.get("retry_after", default)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else default
        delay = float(value)
        return delay if math.isfinite(delay) and delay >= 0 else default
    except (AttributeError, TypeError, ValueError, OverflowError):
        return default


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
    client_msg_id = None
    if "--client-msg-id" in args:
        client_msg_id = args[args.index("--client-msg-id") + 1].strip() or None
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
        post_kwargs = {"channel": channel, "text": text,
                       "thread_ts": thread_ts}
        if client_msg_id:
            # 本次发送尝试的唯一关联证据；Slack 存进消息并在 history
            # 回显，供调用方核验"这次发送"是否成功。
            post_kwargs["client_msg_id"] = client_msg_id
        r = c.chat_postMessage(**post_kwargs)
    except Exception as e:
        if _is_ratelimited(e):
            # 保留服务器给出的完整等待期，由 consumer 持久保存。
            print(json.dumps({"result": "retry_wait",
                              "retry_at": time.time() + _retry_after_seconds(e)}))
            sys.exit(75)
        # 请求可能已被 Slack 接受、只是响应丢失：调用方必须视为不确定，
        # 绝不能当成"未发送"去自动重发。
        print("RESULT uncertain: %s: %s" % (type(e).__name__, e),
              file=sys.stderr)
        sys.exit(2)
    print("sent ok:", r.get("ok"), "ts:", r.get("ts"))


if __name__ == "__main__":
    main()
