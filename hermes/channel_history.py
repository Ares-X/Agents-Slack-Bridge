#!/usr/bin/env python3
"""读取 Slack 频道/线程历史，输出紧凑 JSON lines（统一按时间正序，旧→新）。

零第三方依赖（stdlib urllib），供 Hermes agent 在终端里直接调用来补频道上下文。

用法:
  python3 channel_history.py <channel_id> [limit]                # 频道最近消息
  python3 channel_history.py <channel_id> --thread <ts> [limit]  # 某条消息的 thread
  python3 channel_history.py <channel_id> [limit] --thread <ts>  # 同上（limit 前后都可）
  python3 channel_history.py <channel_id> --resolve              # 顺带把 user ID 解析成名字
  python3 channel_history.py <channel_id> --before-ts <ts> 100    # 补读更早的频道消息

排序说明（以官方文档为准）:
  conversations.history  返回最新在前（倒序）→ 本脚本反转后输出（旧→新）
  conversations.replies  返回父消息 + 回复按时间正序（旧→新）→ 原样输出，不反转

token 解析顺序: 环境变量 SLACK_BOT_TOKEN -> ~/.hermes/.env 里的 SLACK_BOT_TOKEN
失败时输出一行结构化 JSON（{"error": {"kind": ..., "detail": ...}}）并退出码 1，
调用方应如实报告、不编造。token 永不回显、永不写日志。
"""
import argparse
import http.client
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import NoReturn

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


def fail(kind: str, detail: str, retry_after=None) -> NoReturn:
    err = {"error": {"kind": kind, "detail": detail}}
    if retry_after is not None:
        err["error"]["retry_after"] = retry_after
    print(json.dumps(err, ensure_ascii=False))
    sys.exit(1)


def _fail_or_none(kind: str, detail: str, retry_after=None, soft: bool = False):
    """soft=False: 主读取失败 = 硬失败（fail）。soft=True: 可选调用失败 = 降级
    返回 None，不 print、不 exit——调用方继续用已有数据（如保留原 ID）。"""
    if not soft:
        fail(kind, detail, retry_after=retry_after)
    return None


def call(method, token, soft=False, **params):
    """调 Slack Web API。soft=True 时任何失败都不 exit，返回 None（供可选的名称
    解析降级用：名称是锦上添花，失败保留原 ID 即可，绝不能中止主读取流程）。"""
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
    req = urllib.request.Request(
        API + method + "?" + qs,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.code == 429 else None
        _fail_or_none(
            "http_%d" % e.code,
            f"{method}: HTTP {e.code}",
            retry_after=(int(ra) if ra and ra.isdigit() else None) if ra else None,
            soft=soft,
        )
        return None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            _fail_or_none("timeout", f"{method}: request timed out after 30s", soft=soft)
        elif isinstance(reason, ConnectionResetError):
            _fail_or_none("connection_reset", f"{method}: {reason}", soft=soft)
        else:
            _fail_or_none("connection_failed", f"{method}: {reason}", soft=soft)
        return None
    except socket.timeout:
        _fail_or_none("timeout", f"{method}: request timed out after 30s", soft=soft)
        return None
    except http.client.IncompleteRead as e:
        got = getattr(e, "partial", b"") or b""
        _fail_or_none(
            "incomplete_read", f"{method}: connection closed mid-body "
            f"(got {len(got)} bytes)", soft=soft)
        return None
    except ConnectionResetError as e:
        _fail_or_none("connection_reset", f"{method}: {e}", soft=soft)
        return None
    except http.client.HTTPException as e:
        # BadStatusLine / RemoteDisconnected 等协议层异常
        _fail_or_none("http_protocol_error", f"{method}: {type(e).__name__}", soft=soft)
        return None
    try:
        data = json.loads(body)
    except ValueError:
        _fail_or_none("invalid_json", f"{method}: response is not valid JSON (len={len(body)})", soft=soft)
        return None
    if not isinstance(data, dict):
        _fail_or_none("invalid_json", f"{method}: unexpected response shape", soft=soft)
        return None
    if not data.get("ok"):
        detail = data.get("error", "unknown_error")
        # Slack Web API 的 error=ratelimited 同样走这条（429 响应体 ok:false）
        _fail_or_none("slack_api_error", f"{method}: {detail}", soft=soft)
        return None
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("limit", nargs="?", type=int, default=20)
    ap.add_argument("--thread", dest="thread_ts", default=None, help="读这个 ts 的 thread")
    ap.add_argument("--resolve", action="store_true", help="解析 user/bot ID 为显示名")
    ap.add_argument("--before-ts", default=None, help="只读这个 ts 之前的频道消息（不含该条；不能与 --thread 同用）")
    ap.add_argument("--page-info", action="store_true", help="末尾追加分页元数据，明确是否还有更早消息")
    args = ap.parse_intermixed_args()
    if args.thread_ts and args.before_ts:
        fail("invalid_arguments", "--before-ts is for channel history; do not combine it with --thread")

    token = load_token()
    if not token or not token.startswith("xoxb-"):
        fail("missing_token", "SLACK_BOT_TOKEN not found (env or ~/.hermes/.env) or not xoxb-")

    names = {}

    def nm(uid):
        if not uid:
            return ""
        if uid not in names:
            # 前缀区分：U/W = 用户 → users.info；B = bot → bots.info；其他原样保留。
            if uid.startswith("B"):
                info = call("bots.info", token, soft=True, bot=uid)
                names[uid] = (info.get("bot") or {}).get("name", uid) if info else uid
            else:
                info = call("users.info", token, soft=True, user=uid)
                names[uid] = (info.get("user") or {}).get("name", uid) if info else uid
        return names[uid]

    if args.thread_ts:
        # conversations.replies: 父消息 + 回复，时间正序（旧→新）→ 不反转
        method, reverse_output = "conversations.replies", False
        # 注意: 新装的商业分发 App 该接口 limit 上限是 15（2025-05 起）；
        # 内部/自建 App 是 Tier 3。取 min(limit, 200) 但失败时 slack_api_error 会带 detail=ratelimited
    else:
        # conversations.history: 最新在前（倒序）→ 反转成正序（旧→新）
        method, reverse_output = "conversations.history", True

    params = {"channel": args.channel, "limit": min(max(args.limit, 1), 200), "ts": args.thread_ts}
    if args.before_ts:
        params.update(latest=args.before_ts, inclusive="false")
    data = call(method, token, **params)

    msgs = data.get("messages") or []
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

    if reverse_output:
        out.reverse()
    for row in out:  # 统一正序输出（旧→新）
        print(json.dumps(row, ensure_ascii=False))
    if args.page_info:
        print(json.dumps({"page_info": {
            "has_more": bool(data.get("has_more")),
            "next_cursor": (data.get("response_metadata") or {}).get("next_cursor", ""),
            "oldest_ts": out[0]["ts"] if out else "",
            "before_ts": args.before_ts,
        }}, ensure_ascii=False))


if __name__ == "__main__":
    main()
