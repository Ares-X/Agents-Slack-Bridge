#!/usr/bin/env python3
"""slack-mention 第三轮复核回归（PR #10 review round 3）。

三个已复现缺陷的 RED→GREEN 套件（对照 fcd5dd9 必红，修复后全绿）：
  R1  同步渲染工作区绑定：预热 A 后，B 的表为空/查询失败/过期时，
      _maybe_blocks 无上下文不得拿 A 的表猜 B——text 与 blocks 都查。
  R2  流式尾段边界：以完整终稿判定代码围栏与词边界，只改写未发送尾段
      内的提及；已发送前缀字节不动。
  R3  合法空白差异 extends：上游 _stream_relation 第二档（去前缀空白后
      对齐 sent.strip()）也必须解析尾段，且不重复追加/不改写已发送内容。

用法：
  ~/.hermes/hermes-agent/venv/bin/python test_slack_mention_v13_r3.py [repo] [commit]
  默认 repo=~/.hermes/hermes-agent，commit=a5e7df27c7dbcaefd297d85e3792f46d3bc710fc
"""
import asyncio
import importlib.util
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.hermes/hermes-agent"))
COMMIT = sys.argv[2] if len(sys.argv) > 2 else (
    "a5e7df27c7dbcaefd297d85e3792f46d3bc710fc")

FAILS = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def run_async(coro):
    return asyncio.run(coro)


# 0. 从当前 checkout 导入插件（不是装好的那份）
PLUGIN_PATH = os.path.join(HERE, "__init__.py")
check("0. 插件文件在 checkout 内", os.path.isfile(PLUGIN_PATH), PLUGIN_PATH)
spec = importlib.util.spec_from_file_location("slack_mention_r3", PLUGIN_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 1. 固定 commit 的临时 worktree（不碰运行中网关/配置）
if not (REPO / ".git").exists():
    print("ERROR: hermes-agent repo not found:", REPO)
    sys.exit(1)
if subprocess.run(["git", "-C", str(REPO), "cat-file", "-t", COMMIT],
                  capture_output=True).stdout.strip() != b"commit":
    print(f"ERROR: commit not found locally: {COMMIT} (git -C {REPO} fetch origin)")
    sys.exit(1)

WT = Path(tempfile.mkdtemp(prefix="asb-slack-r3-"))
r = subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                    str(WT), COMMIT], capture_output=True, text=True)
if r.returncode != 0:
    shutil.rmtree(WT, ignore_errors=True)
    print("ERROR: worktree add failed:", r.stderr.strip()[:200])
    sys.exit(1)
sys.path.insert(0, str(WT))

import slack_bolt  # noqa: E402,F401
import slack_sdk  # noqa: E402,F401
from slack_sdk.web.slack_response import SlackResponse  # noqa: E402

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.slack.adapter import SlackAdapter as RealAdapter  # noqa: E402


def cleanup():
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(WT)],
                   capture_output=True)


import atexit  # noqa: E402
atexit.register(cleanup)

print("upstream under test:", COMMIT)

# --------------------------------------------------------------------------- 
# A. 上游契约钉子：_stream_relation 的两档 extends 是本轮修复的前提
# --------------------------------------------------------------------------- 
src_rel = inspect.getsource(RealAdapter._stream_relation)
check("A1 上游 _stream_relation 存在且含两档 extends（字节前缀 + 去前缀空白对齐 core）",
      "text.startswith(sent)" in src_rel and "core = sent.strip()" in src_rel
      and "text[lead + len(core):]" in src_rel,
      src_rel[:200])

# --------------------------------------------------------------------------- 
# 公共 harness：真实 SlackAdapter + 垫底 FakeClient（真实 SlackResponse 形状）
# --------------------------------------------------------------------------- 
class FakeClient:
    def __init__(self, pages, team=None, fail_users=False):
        self.pages = pages
        self.team = team
        self.fail_users = fail_users
        self.users_list_calls = 0
        self.posts, self.updates, self.stops, self.starts = [], [], [], []

    async def users_list(self, limit=0, cursor=None):
        self.users_list_calls += 1
        if self.fail_users:
            raise RuntimeError("users.list exploded (review round 3)")
        page = self.pages[min(self.users_list_calls - 1, len(self.pages) - 1)]
        return SlackResponse(
            client=None, http_verb="POST", api_url="users.list", req_args={},
            headers={}, status_code=200,
            data={"members": page["members"],
                  "response_metadata": {"next_cursor": page.get("next_cursor", "")}})

    async def chat_startStream(self, **kw):
        self.starts.append(kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.startStream",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True, "ts": "1715"})

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.postMessage",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True, "ts": "1712"})

    async def chat_update(self, **kw):
        self.updates.append(kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.update",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True})

    async def chat_stopStream(self, **kw):
        self.stops.append(kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.stopStream",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True})

    async def chat_appendStream(self, **kw):
        return SlackResponse(client=None, http_verb="POST", api_url="chat.appendStream",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True})

    async def conversations_open(self, users=None):
        return SlackResponse(client=None, http_verb="POST", api_url="conversations.open",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True, "channel": {"id": "D1"}})


def make_adapter(team_clients):
    ad = RealAdapter(PlatformConfig(extra={"rich_blocks": True}))
    ad._app = type("App", (), {"client": next(iter(team_clients.values()))})()
    ad._team_clients = team_clients
    mod._wrap_adapter(ad)
    return ad


def user_ids(blocks):
    found = []
    stack = list(blocks or [])
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "user":
                found.append(node.get("user_id"))
            stack.extend(node.get("elements") or [])
    return found


def block_texts(blocks):
    texts = []
    stack = list(blocks or [])
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if (node.get("type") or "") == "text" and node.get("text"):
                texts.append(node["text"])
            stack.extend(node.get("elements") or [])
    return texts


MEM_A = [{"id": "U9", "name": "alice",
          "profile": {"display_name": "Alice", "real_name": "A"}}]
MEM_OK = [{"id": "U1", "name": "muse",
           "profile": {"display_name": "Muse", "real_name": "M"}}]


def ok_client(members=None):
    return FakeClient([{"members": members if members is not None else MEM_OK,
                        "next_cursor": ""}])


# --------------------------------------------------------------------------- 
# R1. 同步渲染必须绑定目标工作区：空表/失败表/过期表不许被"唯一非空缓存"挤掉
# --------------------------------------------------------------------------- 
def r1_scenario(client_b, label):
    """预热 A（有 alice→U9），B 的表按 client_b 的行为落缓存；随后向 B 发/编富文本。

    内容用列表语法（"- ping @alice"）逼出 rich_text blocks——纯文本单行
    _maybe_blocks 返回 None，blocks 层检查会空转（round 3 写测试时踩过）。
    send 带 metadata（v1.2 hint 已护），edit 不带 team（纯同步路径）才是
    本轮要堵的洞：blocks 不得引用 A 的 U9。
    """
    clA, clB = ok_client(), client_b
    ad = make_adapter({"TA": clA, "TB": clB})
    run_async(mod._name_table(ad, team_id="TA"))          # A 表就绪（非空）
    run_async(mod._name_table(ad, team_id="TB"))          # B 表：空或失败，也落缓存
    res = run_async(ad.send("CB", "- ping @alice", metadata={"team_id": "TB"}))
    posted = clB.posts[-1] if clB.posts else {}
    check(f"R1[{label}] 向 B 发送（带 team）：text 不引用 A 的用户",
          res.success and "@alice" in posted.get("text", "")
          and "U9" not in posted.get("text", ""),
          repr((res, posted.get("text")))[:300])
    check(f"R1[{label}] 向 B 发送（带 team）：blocks 不引用 A 的用户 ID（U9）",
          posted.get("blocks") and "U9" not in user_ids(posted.get("blocks")),
          repr(user_ids(posted.get("blocks"))))
    # 评审复现路径：编辑不带 team 上下文 → 纯同步渲染 → 不许拿 A 的表猜。
    # （edit 落在哪个 client 由上游 _client_for 路由，与插件无关；断言扫
    # 两个 client 的载荷并集，验证的是「无论路由到哪都不引用 A 的用户」。）
    run_async(ad.edit_message("CB", "1712", "- edit @alice ok", finalize=True))
    all_updates = clA.updates + clB.updates
    all_blocks = [b for u in all_updates for b in (u.get("blocks") or [])]
    check(f"R1[{label}] 向 B 编辑（无 team 上下文）：blocks 不引用 A 的用户 ID",
          all_updates and all_blocks and "U9" not in user_ids(all_blocks),
          repr([user_ids(u.get("blocks")) for u in all_updates]))
    check(f"R1[{label}] 向 B 编辑（无 team 上下文）：@alice 以文本保留（无法确定工作区）",
          any("@alice" in t for t in block_texts(all_blocks)),
          repr([block_texts(u.get("blocks")) for u in all_updates])[:300])


r1_scenario(FakeClient([{"members": [], "next_cursor": ""}]), "空表")
r1_scenario(FakeClient([{"members": MEM_OK, "next_cursor": ""}], fail_users=True), "查询失败")

# R1c 过期刷新：B 的表过期后，唯一新鲜的表是 A（含 alice→U9）——不许猜。
# （两表都健康时 v1.3 已拒猜；本用例才是"唯一幸存者"陷阱。）
adC = make_adapter({
    "TA": FakeClient([{"members": MEM_A, "next_cursor": ""}]),
    "TB": ok_client(),
})
run_async(mod._name_table(adC, team_id="TA"))
run_async(mod._name_table(adC, team_id="TB"))
tables = getattr(adC, mod._TABLES_ATTR)
tables["TB"] = (tables["TB"][0], time.monotonic() - mod._TABLE_TTL - 1)
blkC = adC._maybe_blocks("- hi @alice")
check("R1[过期] B 表过期 + 无上下文：不拿 A 的唯一新鲜表猜",
      blkC and "U9" not in user_ids(blkC)
      and any("@alice" in t for t in block_texts(blkC)),
      repr((user_ids(blkC), block_texts(blkC)))[:300])

# R1d 多工作区同名用户、无上下文：目标工作区无法确定 → 保留原文（守卫）
adD = make_adapter({
    "TA": FakeClient([{"members": [{"id": "U9", "name": "alice"}], "next_cursor": ""}]),
    "TB": FakeClient([{"members": [{"id": "U77", "name": "alice"}], "next_cursor": ""}]),
})
run_async(mod._name_table(adD, team_id="TA"))
run_async(mod._name_table(adD, team_id="TB"))
blkD = adD._maybe_blocks("- hi @alice")
check("R1[同名] 两工作区各有 alice、无上下文：不解析（指谁都可能是错人）",
      blkD and not user_ids(blkD) and any("@alice" in t for t in block_texts(blkD)),
      repr((user_ids(blkD), block_texts(blkD)))[:300])

# --------------------------------------------------------------------------- 
# R2/R3. 流式尾段：完整文本判边界 + 只改未发送部分 + 空白差异 extends
# --------------------------------------------------------------------------- 
def stream_pair(draft, final, team="T1"):
    """send_draft(draft) → send(final)，返回 (client, stops, updates, sendres)。"""
    cl = ok_client()
    ad = make_adapter({team: cl})
    md = {"team_id": team, "thread_id": "7000"}
    d = run_async(ad.send_draft("CS", 1, draft, metadata=md))
    s = run_async(ad.send("CS", final, metadata=md))
    return cl, d, s


def stop_texts(cl):
    return [k.get("markdown_text", "") for k in cl.stops if "markdown_text" in k]


def update_texts(cl):
    return [u.get("text", "") for u in cl.updates]


# —— R2：词边界看完整文本 ——
cl, d, s = stream_pair("support", "support@Muse is great")
check("R2a 词内 @（support + @Muse 跨流式边界）：尾段不解析",
      s.success and stop_texts(cl) == ["@Muse is great"]
      and not any("<@U1>" in t for t in update_texts(cl)),
      repr((stop_texts(cl), update_texts(cl)))[:400])

# —— R2：代码边界看完整文本（reviewer 复现：draft 是开头反引号） ——
cl, d, s = stream_pair("`", "`@Muse` tail")
check("R2b inline 代码跨界（draft='`'，final='`@Muse` tail'）：代码内不解析",
      s.success and stop_texts(cl) == ["@Muse` tail"],
      repr(stop_texts(cl)))

cl, d, s = stream_pair("```\ncode ", "```\ncode @Muse x\n```")
check("R2c 围栏跨界（draft 开栏、final 合栏）：围栏内不解析",
      s.success and stop_texts(cl) == ["@Muse x\n```"],
      repr(stop_texts(cl)))

cl, d, s = stream_pair("```\nps ", "```\nps @Muse aux")
check("R2d 未闭合围栏到 EOF 都是代码：不解析",
      s.success and stop_texts(cl) == ["@Muse aux"],
      repr(stop_texts(cl)))

# —— R2 守卫：正常尾段提及仍解析；跨边界拆名/已发送提及不动 ——
cl, d, s = stream_pair("hey ", "hey @Muse done")
check("R2e 正常尾段提及（词首干净）：仍解析进 delta",
      s.success and stop_texts(cl) == ["<@U1> done"]
      and any("<@U1> done" in t for t in update_texts(cl)),
      repr((stop_texts(cl), update_texts(cl)))[:400])

cl, d, s = stream_pair("done @Mu", "done @Muse x")
check("R2f 提及被流式边界拆开（@Mu + se）：已发送字节不动、整段保留",
      s.success and stop_texts(cl) == ["se x"]
      and any("done @Muse x" in t for t in update_texts(cl)),
      repr((stop_texts(cl), update_texts(cl)))[:400])

cl, d, s = stream_pair("done @Muse", "done @Muse and more")
check("R2g 已发送前缀里的提及不改写、不重复追加",
      s.success and stop_texts(cl) == [" and more"]
      and any(t.count("@Muse") == 1 and "<@U1>" not in t for t in update_texts(cl)),
      repr((stop_texts(cl), update_texts(cl)))[:400])

# —— R3：合法空白差异 extends（reviewer 复现） ——
cl, d, s = stream_pair("Done @Muse  ", "Done @Muse and @Muse")
check("R3a 空白差异 extends：最后的 ' and @Muse' 解析进尾段",
      s.success and stop_texts(cl) == [" and <@U1>"],
      repr(stop_texts(cl)))
check("R3a 已发送前缀保留原文、无重复追加（update 恰一次、前缀字节不动）",
      any("Done @Muse and <@U1>" in t and t.count("<@U1>") == 1
          for t in update_texts(cl)),
      repr(update_texts(cl))[:400])

cl, d, s = stream_pair("Done @Muse", "\n Done @Muse and @Muse")
check("R3b 前导空白变体：前缀（含换行）字节保留，尾段解析",
      s.success and stop_texts(cl) == [" and <@U1>"]
      and any("\n Done @Muse and <@U1>" in t for t in update_texts(cl)),
      repr((stop_texts(cl), update_texts(cl)))[:400])

cl, d, s = stream_pair("Done @Muse  ", "Done @Muse")
check("R3c equal（仅尾部空白差异）：纯封口、无 markdown_text 追加",
      s.success and not stop_texts(cl),
      repr([k for k in cl.stops]))


print()
print(f"checks={CHECKS} fails={len(FAILS)}")
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
print("upstream:", COMMIT)
