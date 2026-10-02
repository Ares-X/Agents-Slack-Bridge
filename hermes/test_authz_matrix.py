#!/usr/bin/env python3
"""授权矩阵回归测试（NOT part of upstream）——验证 README「授权模型」的 12 项断言。

从固定 commit（默认 b3059921bc，与 README 标注一致）checkout 一个临时 git worktree，
在其中加载 SlackAdapter + 网关 authz 逻辑（slack_bolt/slack_sdk 按 upstream
tests/gateway/test_slack_peer_agent_smoke.py 同款方式 mock）。不触碰运行中网关、
~/.hermes 配置或真实 token；worktree 用完即拆。

前提: 本地存在 hermes-agent checkout 且含目标 commit（git cat-file -t <commit> 通过）；
      解释器需有 ruamel（hermes-agent 自带 venv 满足: ~/.hermes/hermes-agent/venv/bin/python）。
用法: python3 test_authz_matrix.py [repo_path] [commit]
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.hermes/hermes-agent"))
COMMIT = sys.argv[2] if len(sys.argv) > 2 else "b3059921bc"


def die(msg: str, code: int = 1):
    print(f'{{"error": "{msg}"}}')
    sys.exit(code)


try:
    import ruamel  # noqa: F401  # hermes_yaml 需要
except ImportError:
    die("this interpreter lacks ruamel; run with: ~/.hermes/hermes-agent/venv/bin/python "
        + " ".join(sys.argv))


if not (REPO / ".git").exists():
    die(f"hermes-agent repo not found: {REPO}")
if subprocess.run(["git", "-C", str(REPO), "cat-file", "-t", COMMIT],
                  capture_output=True).stdout.strip() != b"commit":
    die(f"commit not found locally: {COMMIT} (fetch hermes-agent first)")

WT = Path(tempfile.mkdtemp(prefix="asb-authz-"))
r = subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(WT), COMMIT],
                   capture_output=True, text=True)
if r.returncode != 0:
    shutil.rmtree(WT, ignore_errors=True)
    die(f"worktree add failed: {r.stderr.strip()[:200]}")

sys.path.insert(0, str(WT))
try:
    # --- mock slack 依赖（upstream smoke test 同款） ---
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt), ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler",
         slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk), ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("aiohttp", MagicMock())

    import plugins.platforms.slack.adapter as slack_mod  # noqa: E402
    slack_mod.SLACK_AVAILABLE = True
    from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402
    from gateway.config import PlatformConfig  # noqa: E402
    from gateway.session import SessionSource  # noqa: E402
    from gateway.platforms.base import Platform  # noqa: E402

    PASS, FAIL = [], []

    def check(name, got, want):
        ok = got == want
        (PASS if ok else FAIL).append((name, got, want))
        print(f"{'PASS' if ok else 'FAIL'} {name}: got={got} want={want}")

    def make_adapter(extra=None, allowed_users=""):
        cfg = PlatformConfig(enabled=True, token="***", extra=dict(extra or {}))
        a = SlackAdapter(cfg)
        a._app = MagicMock()
        a._app.client = AsyncMock()
        a._bot_user_id = "U_TARGET"
        a._team_bot_user_ids = {"T_SMOKE": "U_TARGET"}
        a._channel_team = {}
        a._running = True
        a.handle_message = AsyncMock()

        # 网关 runner 在接线时注入的 auth 回调（run_adapters.py `_make_adapter_auth_check`）。
        # 关键复刻: 签名收 is_bot，但 adapter 的早期检查调它时不传（见 C 组断言）。
        def injected_check(user_id, chat_type=None, chat_id=None, *, is_bot=False, thread_id=None):
            src = SessionSource(
                platform=Platform.SLACK, chat_id=chat_id or "C1", chat_type=chat_type or "group",
                user_id=user_id or None, is_bot=bool(is_bot), thread_id=thread_id,
            )
            return FakeRunnerAuthz(allowed_users, extra).is_user_authorized(src)
        a._authorization_check = injected_check
        return a

    class FakeRunnerAuthz:
        """复刻 gateway/authz_mixin.py 对 slack 的裁决顺序:
        _chat_scoped_grant(ALLOW_BOTS: is_bot + allow_bots>=mentions) -> no-user-id guard ->
        allowlist -> default deny。与 _principal_authorized 的真实顺序一致（由
        verify_authz_order.sh 独立钉住）。"""

        def __init__(self, allowed_users="", extra=None):
            self.allowed_users = {u.strip() for u in allowed_users.split(",") if u.strip()}
            self.extra = dict(extra or {})

        def is_user_authorized(self, source):
            if getattr(source, "is_bot", False):
                mode = str(self.extra.get("allow_bots", "none")).lower().strip()
                if mode in {"mentions", "all"}:
                    return True
            if not source.user_id:
                return False
            if source.user_id in self.allowed_users:
                return True
            return False

    async def main():
        print(f"== A/B: no SLACK_ALLOWED_USERS (commit {COMMIT[:10]}) ==")
        a = make_adapter(extra={"allow_bots": "mentions"}, allowed_users="")
        check("A human denied when no allowlist",
              a._early_reject_unauthorized("UOWNER", "C1", False), True)
        ev = {"type": "message", "subtype": "bot_message", "bot_id": "B999", "channel": "C1",
              "text": "workflow <@U_TARGET>", "ts": "123.004"}
        check("B _event_declares_bot_sender(bot_message,no user)",
              a._event_declares_bot_sender(ev), True)
        check("B _drop_bot_sender(mentions, mentions us)", await a._drop_bot_sender(ev), False)
        src = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="group",
                            user_id=None, is_bot=True)
        check("B gateway grants no-user-id bot (allow_bots=mentions)",
              FakeRunnerAuthz("", {"allow_bots": "mentions"}).is_user_authorized(src), True)

        print("== C: allowlist set, peer bot WITH user_id, client_msg_id absent ==")
        a2 = make_adapter(extra={"allow_bots": "mentions"}, allowed_users="UOWNER")
        ev2 = {"type": "message", "channel": "C1", "user": "U_PEER_BOT",
               "text": "hi <@U_TARGET>", "ts": "123.005", "app_id": "A2", "thread_ts": None}
        check("C _event_declares_bot_sender(app_id,no client_msg_id)",
              a2._event_declares_bot_sender(ev2), True)
        # 早期检查对注入回调不传 is_bot -> 走人类 allowlist 判决 -> 拒
        check("C early reject peer-bot user_id",
              a2._early_reject_unauthorized("U_PEER_BOT", "C1", False), True)

        print("== D: allowlist set, classic bot post (bot_id, no user field) ==")
        ev3 = {"type": "message", "subtype": "bot_message", "bot_id": "B777", "channel": "C1",
               "text": "hey <@U_TARGET>", "ts": "123.006"}
        check("D no user_id -> early check skipped",
              a2._early_reject_unauthorized("", "C1", False), False)
        check("D _drop_bot_sender(mentions, mentions us)", await a2._drop_bot_sender(ev3), False)
        src3 = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="group",
                             user_id=None, is_bot=True)
        check("D gateway grants classic bot (allow_bots=mentions)",
              FakeRunnerAuthz("UOWNER", {"allow_bots": "mentions"}).is_user_authorized(src3), True)

        print("== E: allow_bots=none + bot post -> dropped at adapter ==")
        a3 = make_adapter(extra={"allow_bots": "none"}, allowed_users="UOWNER")
        check("E _drop_bot_sender(none)", await a3._drop_bot_sender(ev3), True)

        print("== F: own echo (bot's own user) never re-enters ==")
        a4 = make_adapter(extra={"allow_bots": "all"}, allowed_users="")
        ev5 = {"type": "message", "channel": "C1", "user": "U_TARGET", "text": "self",
               "ts": "123.007", "bot_id": "B_TARGET"}
        check("F _drop_bot_sender(all, own user)", await a4._drop_bot_sender(ev5), True)

        print("== G: allow_bots=mentions + bot post WITHOUT mention -> dropped at adapter ==")
        ev6 = {"type": "message", "subtype": "bot_message", "bot_id": "B777", "channel": "C1",
               "text": "no mention here", "ts": "123.008"}
        check("G _drop_bot_sender(mentions, no mention)", await a2._drop_bot_sender(ev6), True)

        print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        return 1 if FAIL else 0

    rc = asyncio.run(main())
finally:
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(WT)],
                   capture_output=True)
    shutil.rmtree(WT, ignore_errors=True)

sys.exit(rc)
