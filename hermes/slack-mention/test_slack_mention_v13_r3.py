#!/usr/bin/env python3
"""slack-mention 第三/四轮复核回归（PR #10 review round 3 + r4）。

RED→GREEN 套件（对照 fcd5dd9 必红，修复后全绿）：
  R1  同步渲染工作区绑定：预热 A 后，B 的表为空/查询失败/过期时，
      _maybe_blocks 无上下文不得拿 A 的表猜 B——text 与 blocks 都查。
      （r4 #3 夹具修正：本场景应加载 MEM_A，此前 ok_client() 装的是
      Muse/U1，断言查 Alice/U9 永远空转。）
  R2  流式尾段边界：以完整终稿判定代码围栏与词边界，只改写未发送尾段
      内的提及；已发送前缀字节不动。
  R3  合法空白差异 extends：上游 _stream_relation 第二档（去前缀空白后
      对齐 sent.strip()）也必须解析尾段，且不重复追加/不改写已发送内容。
  R4  评审第四轮三缺陷：
      #1 只预热次工作区（B），无 metadata 发送/编辑 → 上游 primary
         fallback 路由到 A 的客户端；唯一幸存的 B 表不能证明目标工作区，
         text 与 blocks 都保留原文。
      #2 同 ts 并发收尾互不干扰：A 工作区流收尾等待期间——B 工作区同
         ts 流收尾不受 A 完成/撤销影响（保护不丢）；B 的同 ts 无关普通
         编辑照常解析（不被 A 的直通窗口错误跳过）。对照组：不同 ts。
      RED 对照：同套测试在旧缺陷版本（fcd5dd9 基础上仅修 R1 夹具）必失败。

用法：
  ~/.hermes/hermes-agent/venv/bin/python test_slack_mention_v13_r3.py [repo] [commit]
  默认 repo=~/.hermes/hermes-agent，commit=a5e7df27c7dbcaefd297d85e3792f46d3bc710fc
"""
import asyncio
import importlib.util
import inspect
import json
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


def ok_client_a():
    """R1 场景专用：工作区 A 的健康表（alice→U9）——r4 #3 夹具修正。"""
    return FakeClient([{"members": MEM_A, "next_cursor": ""}])


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
    clA, clB = ok_client_a(), client_b
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


# ===========================================================================
# R4：评审第四轮复核（PR #10 r4）
# ===========================================================================
# 公共夹具：A/B 双工作区。make_adapter 把 _app.client 挂在**第一个**键上：
# make_adapter({TA, TB}) → primary=TA，上游 _get_client 无上下文最终档
# `return self._app.client` → 无 metadata 消息实际路由到 TA 的客户端。
MEM_A4 = [{"id": "U9", "name": "alice",
           "profile": {"display_name": "Alice", "real_name": "A"}}]
MEM_B4 = [{"id": "U1", "name": "muse",
           "profile": {"display_name": "Muse", "real_name": "M"}}]


def r4_setup():
    """A/B 双工作区，只预热 B（次工作区）的表。返回 (ad, clA, clB)。"""
    clA = FakeClient([{"members": MEM_A4, "next_cursor": ""}])
    clB = FakeClient([{"members": MEM_B4, "next_cursor": ""}])
    ad = make_adapter({"TA": clA, "TB": clB})
    run_async(mod._name_table(ad, team_id="TB"))   # B 表健康；A 表从未建过
    return ad, clA, clB


def _blocks_str(payload):
    return json.dumps(payload.get("blocks") or [], ensure_ascii=False)


# —— R4-1：只预热次工作区，无 metadata 发送/编辑 → text 与 blocks 都保留原文 ——
# 提及用 @muse（在 B 的预热表里）：旧缺陷版 text 保留原文（async 拒猜）但
# blocks 拿唯一幸存的 B 表解析成 <@U1>——同一条消息两处指人不一致。
ad, clA, clB = r4_setup()
res = run_async(ad.send("CX", "hi @muse from B", metadata=None))
posted = clA.posts[-1] if clA.posts else {}   # primary=TA，无上下文发去 A 的客户端
check("R4a 无上下文发送：路由到 primary（TA）客户端，不去 B",
      bool(clA.posts) and not clB.posts,
      f"A.posts={len(clA.posts)} B.posts={len(clB.posts)}")
check("R4a 无上下文发送：text 保留 @muse 原文",
      posted.get("text", "") == "hi @muse from B",
      repr(posted.get("text"))[:200])
check("R4a 无上下文发送：唯一缓存表(TB)≠primary(TA) → blocks 同样保留原文",
      bool(posted.get("blocks")) and "<@U1>" not in _blocks_str(posted),
      _blocks_str(posted)[:200])

ad, clA, clB = r4_setup()
run_async(ad.edit_message("CX", "1777", "edit @muse no-md", finalize=True))
upd = clA.updates[-1] if clA.updates else {}
check("R4b 无上下文编辑（finalize）：text 保留 @muse 原文",
      upd.get("text", "") == "edit @muse no-md",
      repr(upd.get("text"))[:200])
check("R4b 无上下文编辑（finalize）：blocks 同样保留原文（不引 TB 的 U1）",
      "<@U1>" not in _blocks_str(upd),
      _blocks_str(upd)[:200])

# 对照：明确 A 目标（metadata TA）时照常解析——「目标不明才不猜」不是全禁。
ad, clA, clB = r4_setup()
res = run_async(ad.send("CX", "hi @alice from A", metadata={"team_id": "TA"}))
posted = clA.posts[-1] if clA.posts else {}
check("R4c 对照：metadata 指明 TA → 正常解析（<@U9>）",
      "<@U9>" in posted.get("text", ""),
      repr(posted.get("text"))[:200])

# 对照：单工作区主场景不受影响（唯一键 == primary）。
ad1 = make_adapter({"TA": FakeClient([{"members": MEM_A4, "next_cursor": ""}])})
run_async(mod._name_table(ad1, team_id="TA"))
res = run_async(ad1.send("CX", "hi @alice single", metadata=None))
posted1 = ad1._app.client.posts[-1] if res.success else {}
check("R4d 对照：单工作区无上下文 → 唯一表==primary，照常解析",
      "<@U9>" in posted1.get("text", ""),
      repr(posted1.get("text"))[:200])


# —— R4-2：同 ts 并发收尾互不干扰 ——
class GatedClient(FakeClient):
    """可分别门闩 chat_stopStream / chat_update——复现收尾窗口内的时序交错。

    A 停在自己收尾 edit 内（过了直通判定、chat.update 未返回）；B 停在
    seal（还没到 edit 判定）——这正是旧版全局 set(ts) 互踩的时序形状。
    """
    def __init__(self, pages, gate_seal=False, gate_update=False):
        super().__init__(pages)
        self.seal_gate, self.update_gate = asyncio.Event(), asyncio.Event()
        self.gate_seal, self.gate_update = gate_seal, gate_update
        if not gate_seal:
            self.seal_gate.set()
        if not gate_update:
            self.update_gate.set()

    async def chat_stopStream(self, **kw):
        await self.seal_gate.wait()
        return await super().chat_stopStream(**kw)

    async def chat_update(self, **kw):
        await self.update_gate.wait()
        return await super().chat_update(**kw)


async def _settle(seconds=0.05):
    for _ in range(10):
        await asyncio.sleep(seconds / 10)


async def concurrent_finalize():
    # A：TA/CA/ts=1111，update 门闩；B：TB/CB/ts=1111（同 ts），seal 门闩。
    clA = GatedClient([{"members": MEM_A4, "next_cursor": ""}], gate_update=True)
    clB = GatedClient([{"members": MEM_B4, "next_cursor": ""}], gate_seal=True)
    ad = make_adapter({"TA": clA, "TB": clB})
    await mod._name_table(ad, team_id="TA")
    await mod._name_table(ad, team_id="TB")
    mdA = {"team_id": "TA", "thread_id": "1111"}
    mdB = {"team_id": "TB", "thread_id": "1111"}
    taskA = asyncio.create_task(ad._commit_stream(
        ("TA", "CA", "1111"), {"ts": "1111", "sent": "done @alice"},
        "done @alice tail", mdA, delta=" tail", replace=False))
    taskB = asyncio.create_task(ad._commit_stream(
        ("TB", "CB", "1111"), {"ts": "1111", "sent": "done @muse"},
        "done @muse tail", mdB, delta=" tail", replace=False))
    await _settle()   # A 停在收尾 edit 内；B 停在 seal
    # 窗口内：B 工作区对同 ts（1111）的无关普通编辑照常解析——不被 A 的
    # 直通窗口错误跳过（旧版：全局 set 里有 1111 → 跳过 ✗）。
    await ad.edit_message("CB", "1111", "unrelated @muse edit", metadata=mdB)
    upd_unrelated = clB.updates[-1] if clB.updates else {}
    # A 先放行完成：旧版 finally 从全局 set 撤销 1111 = 连 B 的保护一起撤；
    # B 随后过 seal 到达 edit 判定时集合已空 → 全文解析 ✗。
    clA.update_gate.set()
    await taskA
    updA = clA.updates[-1] if clA.updates else {}
    clB.seal_gate.set()
    await taskB
    updB = clB.updates[-1] if clB.updates else {}
    return upd_unrelated, updA, updB


upd_unrelated, updA, updB = run_async(concurrent_finalize())
check("R4e 窗口内同 ts 无关编辑照常解析（不被 A 的直通窗口跳过）",
      upd_unrelated.get("text") == "unrelated <@U1> edit",
      repr(upd_unrelated)[:300])
check("R4f A 先完成不撤销 B 的保护：B 收尾 update 的已发送前缀不被改写",
      updB.get("text") == "done @muse tail",
      repr(updB)[:300])
check("R4g A 自身收尾正常（尾段无提及、前缀不动）",
      updA.get("text") == "done @alice tail",
      repr(updA)[:300])


# —— R4-2 对照：不同 ts 的无关编辑（非流目标）照常解析 ——
async def different_ts_finalize():
    # 对照组用 seal 门闩（收尾停在 stopStream，无关编辑的 chat_update 自由）：
    # 同一客户端上 update 门闩会把无关编辑一起挡住，测的就是「编辑不被跳过」。
    clA = GatedClient([{"members": MEM_A4, "next_cursor": ""}], gate_seal=True)
    ad = make_adapter({"TA": clA})
    await mod._name_table(ad, team_id="TA")
    md = {"team_id": "TA", "thread_id": "3333"}
    task = asyncio.create_task(ad._commit_stream(
        ("TA", "CA", "3333"), {"ts": "3333", "sent": "keep @alice"},
        "keep @alice tail", md, delta=" tail", replace=False))
    await _settle()
    await ad.edit_message("CA", "4444", "other @alice msg", metadata=md)
    upd_other = clA.updates[-1] if clA.updates else {}
    clA.seal_gate.set()
    await task
    upd_final = clA.updates[-1] if clA.updates else {}
    return upd_other, upd_final


upd_other, upd_final = run_async(different_ts_finalize())
check("R4h 不同 ts 无关编辑照常解析（对照）",
      upd_other.get("text") == "other <@U9> msg",
      repr(upd_other)[:300])
check("R4i 流收尾自身照常：尾段无提及时前缀原文直达（对照）",
      upd_final.get("text") == "keep @alice tail",
      repr(upd_final)[:300])


class PlainClient(FakeClient):
    """chat.stopStream 恒失败：逼出 _commit_stream 的恢复路径（chat.update 补投）。

    纯文本部署（rich_blocks=False）下 orig _maybe_blocks 恒 None——正是评审
    第五轮 P2 的回归现场：r4 的「final_blocks 探测」把它误判成「text 缺解析」
    又跑了全文 _resolve_async_outlet，已发送前缀里的 @name 被二次改写。
    """

    async def chat_stopStream(self, **kw):
        self.stops.append(kw)
        raise RuntimeError("stopStream down (review round 5)")


def plain_adapter(team="T1"):
    """rich_blocks=False 的真实适配器（纯文本部署），已预热 TA 表。"""
    cl = PlainClient([{"members": MEM_A4, "next_cursor": ""}])
    ad = RealAdapter(PlatformConfig(extra={"rich_blocks": False}))
    ad._app = type("App", (), {"client": cl})()
    ad._team_clients = {team: cl}
    mod._wrap_adapter(ad)
    run_async(mod._name_table(ad, team_id=team))
    return ad, cl


# R5a 复现（P2 正文场景）：draft 已含已发送提及，终稿尾段再提一次同名——
# 恢复 edit 必须保留前缀原文、只解析尾段。r4 缺陷：全文重解析把前缀里的
# @Alice 也改成 <@U9>（"Done <@U9> and <@U9>"）；2bda701 保持前缀不动。
adP, clP = plain_adapter()
mdP = {"team_id": "T1", "thread_id": "5555"}
run_async(adP.send_draft("CP", 1, "Done @Alice", metadata=mdP))
sP = run_async(adP.send("CP", "Done @Alice and @Alice", metadata=mdP))
rec = clP.updates[-1] if clP.updates else {}
check("R5a 纯文本 stopStream 失败：恢复 edit 保留已发送前缀（@Alice 不二次改写）",
      sP.success and rec.get("text") == "Done @Alice and <@U9>",
      repr((rec, clP.updates))[:400])

# R5b 复现（流式边界拆名变体）：draft 以半个提及收尾，恢复 edit 不得把它
# 补写成实体（词内 @ 不是提及，前缀字节已上屏）。
adQ, clQ = plain_adapter()
mdQ = {"team_id": "T1", "thread_id": "6666"}
run_async(adQ.send_draft("CQ", 1, "Done @Al", metadata=mdQ))
sQ = run_async(adQ.send("CQ", "Done @Alice tail", metadata=mdQ))
recQ = clQ.updates[-1] if clQ.updates else {}
check("R5b 边界拆名：恢复 edit 保留被切开的提及原文（@Alice 不进实体）",
      sQ.success and recQ.get("text") == "Done @Alice tail",
      repr(recQ)[:400])

# R5c 恢复 edit 不重复发布：收尾失败后没有新 chat.postMessage（一个答案）。
check("R5c 恢复 edit 不重复发布（无新 post）",
      not clP.posts and not clQ.posts,
      f"posts={len(clP.posts)},{len(clQ.posts)}")


# —— R5-2：取消清理（回归保留）——
async def cancelled_finalize():
    # 收尾停在恢复 edit 内（update 门闩）时任务被取消：finally 必须撤销
    # contextvar 登记，否则同上下文后续同键编辑永远直通（解析被吞）。
    cl = GatedClient([{"members": MEM_A4, "next_cursor": ""}], gate_update=True)
    ad = RealAdapter(PlatformConfig(extra={"rich_blocks": False}))
    ad._app = type("App", (), {"client": cl})()
    ad._team_clients = {"T1": cl}
    mod._wrap_adapter(ad)
    await mod._name_table(ad, team_id="T1")
    md = {"team_id": "T1", "thread_id": "7777"}
    task = asyncio.create_task(ad._commit_stream(
        ("T1", "CC", "7777"), {"ts": "7777", "sent": "keep @Alice"},
        "keep @Alice tail", md, delta=" tail", replace=False))
    await _settle()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    cl.update_gate.set()   # 打开门闩，让后续普通编辑能走通 chat_update
    # 取消后同一事件循环内的普通编辑必须恢复解析（登记已被 finally 撤销）。
    await ad.edit_message("CC", "7777", "later @Alice edit", metadata=md)
    return cl.updates[-1] if cl.updates else {}


rec_cancel = run_async(cancelled_finalize())
check("R5d 取消清理：取消后 contextvar 登记撤销，后续编辑照常解析",
      rec_cancel.get("text") == "later <@U9> edit",
      repr(rec_cancel)[:400])


print()
print(f"checks={CHECKS} fails={len(FAILS)}")
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
print("upstream:", COMMIT)
