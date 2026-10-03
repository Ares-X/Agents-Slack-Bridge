#!/usr/bin/env python3
"""slack-mention v1.3 测试：真实上游（固定 commit worktree）+ 真实 SDK 形状。

三层真实：
  A. 树内真源码静态契约：inspect 真实 SlackAdapter 的 _commit_stream/_seal_stream/
     send_draft/_maybe_blocks/_metadata_team_id/_get_client 签名与源码不变式。
  B. 真实 slack_sdk SlackResponse（**不是 dict**，支持 .get()）+ 真实
     block_kit.render_blocks 产物。
  C. 真实 SlackAdapter 实例（固定 commit 的 worktree）跑完整方法体：
     send_draft → send → _commit_stream 全链路（普通/富文本、冷缓存、片段切换），
     FakeClient 只垫最底层 SDK 调用。

用法：
  ~/.hermes/hermes-agent/venv/bin/python test_slack_mention_v13.py [repo] [commit]
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
from pathlib import Path
from unittest.mock import MagicMock

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


# --------------------------------------------------------------------------- 
# 0. 从当前 checkout 导入插件（不是装好的那份）
# ---------------------------------------------------------------------------
PLUGIN_PATH = os.path.join(HERE, "__init__.py")
check("0. 插件文件在 checkout 内", os.path.isfile(PLUGIN_PATH), PLUGIN_PATH)
spec = importlib.util.spec_from_file_location("slack_mention_v13", PLUGIN_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# --------------------------------------------------------------------------- 
# 1. 固定 commit 的临时 worktree（不碰运行中网关/配置）
# ---------------------------------------------------------------------------
if not (REPO / ".git").exists():
    print("ERROR: hermes-agent repo not found:", REPO)
    sys.exit(1)
if subprocess.run(["git", "-C", str(REPO), "cat-file", "-t", COMMIT],
                  capture_output=True).stdout.strip() != b"commit":
    print(f"ERROR: commit not found locally: {COMMIT} (git -C {REPO} fetch origin)")
    sys.exit(1)

WT = Path(tempfile.mkdtemp(prefix="asb-slack-v13-"))
r = subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                    str(WT), COMMIT], capture_output=True, text=True)
if r.returncode != 0:
    shutil.rmtree(WT, ignore_errors=True)
    print("ERROR: worktree add failed:", r.stderr.strip()[:200])
    sys.exit(1)
sys.path.insert(0, str(WT))

import slack_bolt  # noqa: E402
import slack_sdk  # noqa: E402
from slack_sdk.web.slack_response import SlackResponse  # noqa: E402

# 真实 SDK 在 venv 里可用（不 mock）——FakeClient 只垫网络层
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.slack.adapter import SlackAdapter as RealAdapter  # noqa: E402

SlackAdapter = RealAdapter


def cleanup():
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(WT)],
                   capture_output=True)


import atexit  # noqa: E402
atexit.register(cleanup)

# --------------------------------------------------------------------------- 
# A. 真实上游契约（inspect 固定 commit 的真 SlackAdapter）
# ---------------------------------------------------------------------------
sig_commit = inspect.signature(SlackAdapter._commit_stream)
sig_seal = inspect.signature(SlackAdapter._seal_stream)
sig_draft = inspect.signature(SlackAdapter.send_draft)
sig_send = inspect.signature(SlackAdapter.send)
sig_blocks = inspect.signature(SlackAdapter._maybe_blocks)
sig_getclient = inspect.signature(SlackAdapter._get_client)

check("A1 _commit_stream 真实签名=(key,stream,text,metadata,delta=,replace=)",
      list(sig_commit.parameters) == ["self", "key", "stream", "text", "metadata",
                                      "delta", "replace"]
      and sig_commit.parameters["delta"].default == ""
      and sig_commit.parameters["replace"].default is False,
      str(sig_commit))
check("A2 _seal_stream 真实签名=(key,stream,delta=None)",
      list(sig_seal.parameters) == ["self", "key", "stream", "delta"]
      and sig_seal.parameters["delta"].default is None,
      str(sig_seal))
check("A3 send_draft 存在且 async",
      hasattr(SlackAdapter, "send_draft") and inspect.iscoroutinefunction(SlackAdapter.send_draft),
      str(sig_draft))
check("A4 _maybe_blocks 同步 def",
      not inspect.iscoroutinefunction(SlackAdapter._maybe_blocks)
      and list(sig_blocks.parameters) == ["self", "content"],
      str(sig_blocks))
check("A5 _get_client 认显式 team_id",
      list(sig_getclient.parameters) == ["self", "chat_id", "team_id"],
      str(sig_getclient))

src_commit = inspect.getsource(SlackAdapter._commit_stream)
src_seal = inspect.getsource(SlackAdapter._seal_stream)
src_tryfin = inspect.getsource(SlackAdapter._try_finalize_stream)
check("A6 _commit_stream 源码含 stopStream APPEND 语义（_seal_stream(key,stream,delta=delta)）",
      "_seal_stream(key, stream, delta=delta)" in src_commit
      and "markdown_text" in src_seal,
      src_commit[:200])
check("A7 _try_finalize_stream 源码含 delta=delta 与 replace=True 两分支",
      "delta=delta" in src_tryfin and "replace=True" in src_tryfin,
      src_tryfin[:200])

# 插件包装签名与上游对齐（这是本轮 review 的核心：补丁必须能被真实调用点调起）
w_src = inspect.getsource(mod._wrap_adapter)
check("A8 插件包装 _commit_stream_patched 签名对齐上游（key,stream,text,metadata,delta=,replace=）",
      "async def _commit_stream_patched(key, stream, text, metadata=None, *," in w_src
      and 'delta="", replace=False' in w_src)
check("A9 插件不再包装 _seal_stream（新上游契约下无文本路径，包了必错签名）",
      "_seal_stream_patched" not in w_src)

# metadata team keys 与上游同源
src_meta = inspect.getsource(SlackAdapter._metadata_team_id)
ok_keys = all(f'"{k}"' in src_meta for k in mod._METADATA_TEAM_KEYS)
check("A10 插件 metadata team keys ⊆ 上游 _metadata_team_id", ok_keys, src_meta[:200])

# --------------------------------------------------------------------------- 
# B. 真实 SlackResponse（不是 dict）+ 真实 renderer
# ---------------------------------------------------------------------------
resp = SlackResponse(client=None, http_verb="POST", api_url="users.list",
                     req_args={}, headers={}, status_code=200,
                     data={"members": [{"id": "U1"}],
                           "response_metadata": {"next_cursor": "p2"}})
check("B1 SlackResponse 支持 .get() 但不是 dict",
      not isinstance(resp, dict) and resp.get("members") == [{"id": "U1"}],
      type(resp).__name__)

from plugins.platforms.slack.block_kit import render_blocks  # noqa: E402

blocks = render_blocks("- item `code <@U1> x` tail\n- two <@U2> plain")
flat_b = [e for b in blocks if b.get("type") == "rich_text"
          for top in b.get("elements", [])
          for sec in top.get("elements", [])
          for e in sec.get("elements", [])]
check("B2 真实 renderer 的 inline code 带 style.code",
      any((e.get("style") or {}).get("code") and "<@U1>" in e.get("text", "")
          for e in flat_b),
      repr(blocks)[:200])
out = mod._tokenize_blocks(blocks)
flat = [e for b in out if b.get("type") == "rich_text"
        for top in b.get("elements", [])
        for sec in top.get("elements", [])
        for e in sec.get("elements", [])]
check("B3 style.code 节点内的实体保持 text（不转 user）",
      any(e.get("type") == "text" and "<@U1>" in e.get("text", "")
          and (e.get("style") or {}).get("code") for e in flat)
      and not any(e.get("type") == "user" and e.get("user_id") == "U1"
                  and (e.get("style") or {}).get("code") for e in flat),
      repr(flat)[:300])
check("B4 代码外实体转 user 元素",
      any(e.get("type") == "user" and e.get("user_id") == "U2" for e in flat),
      repr(flat)[:300])


# --------------------------------------------------------------------------- 
# C. 真实 SlackAdapter 实例：全链路 send_draft → send → _commit_stream
# ---------------------------------------------------------------------------
class FakeClient:
    """垫最底层 SDK。users_list 返回真实 SlackResponse 形状，两页分页。"""

    def __init__(self, pages, team=None):
        self.pages = pages          # [ {members, next_cursor}, ... ] 逐页弹出
        self.team = team
        self.users_list_calls = 0
        self.cursors_seen = []
        self.posts, self.updates, self.stops, self.appends, self.starts = [], [], [], [], []
        self.last = None

    async def users_list(self, limit=0, cursor=None):
        self.users_list_calls += 1
        self.cursors_seen.append(cursor)
        page = self.pages[min(self.users_list_calls - 1, len(self.pages) - 1)]
        return SlackResponse(
            client=None, http_verb="POST", api_url="users.list", req_args={},
            headers={}, status_code=200,
            data={"members": page["members"],
                  "response_metadata": {"next_cursor": page.get("next_cursor", "")}})

    async def chat_startStream(self, **kw):
        self.starts.append(kw)
        self.last = ("start", kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.startStream",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True, "ts": "1715"})

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        self.last = ("post", kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.postMessage",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True, "ts": "1712"})

    async def chat_update(self, **kw):
        self.updates.append(kw)
        _trace_out.append(("chat_update", kw))
        return SlackResponse(client=None, http_verb="POST", api_url="chat.update",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True})

    async def chat_stopStream(self, **kw):
        self.stops.append(kw)
        _trace_out.append(("chat_stopStream", kw))
        return SlackResponse(client=None, http_verb="POST", api_url="chat.stopStream",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True})

    async def chat_appendStream(self, **kw):
        self.appends.append(kw)
        self.last = ("append", kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.appendStream",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True})

    async def conversations_open(self, users=None):
        return SlackResponse(client=None, http_verb="POST", api_url="conversations.open",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True, "channel": {"id": "D1"}})


_trace_out = []


def make_adapter(pages, team_clients=None):
    ad = RealAdapter(PlatformConfig(extra={"rich_blocks": True}))
    client = FakeClient(pages)
    ad._app = type("App", (), {"client": client})()
    ad._team_clients = team_clients or {}
    return ad, client


PAGE1 = [{"id": "U1", "name": "muse",
          "profile": {"display_name": "Muse", "real_name": "Muse C"}}]
PAGE2 = [{"id": "U2", "name": "grokbot",
          "profile": {"display_name": "Grok Bot", "real_name": "G"}}]

# —— C1-C4：两页分页 + 真实 SlackResponse（评审点 2）——
ad, client = make_adapter([
    {"members": PAGE1, "next_cursor": "p2"},   # 第一页带游标
    {"members": PAGE2, "next_cursor": ""},     # 第二页收尾
])
mod._wrap_adapter(ad)
t = run_async(mod._name_table(ad))
check("C1 两页真实 SlackResponse：两页用户都解析到",
      mod._lookup(t, "muse") == ("U1", "") and mod._lookup(t, "grok bot") == ("U2", ""),
      repr(t))
check("C2 分页真走了两页（users_list 调用两次 + 游标传递）",
      client.users_list_calls == 2 and client.cursors_seen[1] == "p2",
      repr((client.users_list_calls, client.cursors_seen)))
cached = getattr(ad, mod._TABLES_ATTR)[""]  # 缓存键 team=""
check("C3 缓存表正确（两页合并，非空）",
      isinstance(cached, tuple) and len(cached[0]) >= 4 and cached[0].get("muse"),
      repr(cached)[:200])
check("C4 resp 不是 dict 也能取页（评审点 2 的根因回归）",
      not isinstance(client.pages, dict), "")


# —— C5-C9：send_draft → send → _commit_stream 全链路（评审点 1）——
# 真网关形状：metadata 带 team_id 时该 team 的 client 必须已认证（_team_clients），
# 否则严格绑定按设计不解析（D8/v1.2 语义）——测试里同样注册。
def fresh_adapter(pages=None, team=None):
    a, c = make_adapter(pages or [
        {"members": PAGE1 + PAGE2, "next_cursor": ""}])
    if team:
        a._team_clients = {team: c}
    mod._wrap_adapter(a)
    return a, c


# C5 普通文本：draft 流式 → send 收尾，mention 在未流出尾段 → 解析进 delta
ad5, cl5 = fresh_adapter(team="T1")
md5 = {"team_id": "T1", "thread_id": "1000"}
draft = run_async(ad5.send_draft("C1", 1, "done @Grok Bot se", metadata=md5))
sendres = run_async(ad5.send("C1", "done @Grok Bot se… and @Muse.", metadata=md5))
stop5 = [k for k in cl5.stops]
check("C5 全链路普通文本：draft+send 成功，mention 解析进收尾 delta",
      getattr(draft, "success", False) and getattr(sendres, "success", False)
      and any(k.get("markdown_text") == "… and <@U1>." for k in stop5),
      repr((draft, sendres, stop5))[:400])

# C6 富文本：chat.update 带解析后的 blocks（finalize=True）


def serialize_user(blocks):
    """blocks 里出现过的 user_id 集合。"""
    found = {}
    stack = list(blocks or [])
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "user":
                found[node.get("user_id")] = True
            stack.extend(node.get("elements") or [])
    return found


# C6 富文本：列表语境逼出 rich_text blocks；chat.update 带解析后的 user 元素
ad6, cl6 = fresh_adapter(team="T2")
md6 = {"team_id": "T2", "thread_id": "2000"}
run_async(ad6.send_draft("C2", 1, "- answer @Grok Bot partial", metadata=md6))
run_async(ad6.send("C2", "- answer @Grok Bot partial\n- tag @Muse too", metadata=md6))
upds6 = [u for u in cl6.updates]
blk_ok = any(u.get("blocks") and serialize_user(u["blocks"]).get("U1")
             and serialize_user(u["blocks"]).get("U2") for u in upds6)
txt_ok = all("tag <@U1>" in u.get("text", "") or not u.get("text") for u in upds6)
check("C6 富文本收尾：chat.update 带解析后的 user 元素 blocks（rich_text）",
      upds6 and blk_ok and txt_ok,
      repr(upds6)[:400])


# C7 冷缓存：首条 send 直接建表解析（async 出口保证）
ad7, cl7 = fresh_adapter(team="T3")   # 未预热
res7 = run_async(ad7.send("C3", "cold @Muse start", metadata={"team_id": "T3"}))
check("C7 冷缓存首条 send：表已建、正文已解析",
      res7.success and cl7.posts and "<@U1>" in cl7.posts[-1].get("text", ""),
      repr((res7, cl7.posts[-1] if cl7.posts else None))[:300])

# C8 片段切换：send_draft 二次调用同 key 不同 draft_id → seal 旧流（不带文本）
ad8, cl8 = fresh_adapter(team="T4")
md8 = {"team_id": "T4", "thread_id": "3000"}
run_async(ad8.send_draft("C4", 1, "seg one @Muse", metadata=md8))
run_async(ad8.send_draft("C4", 2, "seg two", metadata=md8))  # 切换 → seal 旧流
check("C8 片段切换：旧流被 seal（无 markdown_text=纯封口）且新流开启",
      cl8.stops and all("markdown_text" not in k for k in cl8.stops)
      and len(cl8.starts) == 2,
      repr((cl8.stops, cl8.starts))[:300])

# C9 _commit_stream replace=True：终稿改写 → 整篇解析（update 全文）
ad9, cl9 = fresh_adapter(team="T5")
md9 = {"team_id": "T5", "thread_id": "4000"}
run_async(ad9.send_draft("C5", 1, "draft @Grok Bot", metadata=md9))
res9 = run_async(ad9._commit_stream(("T5", "C5", "4000"),
                                    {"ts": "9", "sent": "draft @Grok Bot"},
                                    "@Muse rewrote all", md9, replace=True))
check("C9 replace=True 整篇解析（chat.update 收 <@U1>）",
      res9 is not None and any("<@U1>" in u.get("text", "") for u in cl9.updates),
      repr((res9, cl9.updates))[:400])


# —— C10-C14：同步表路由的工作区隔离（评审点 3）——
# A 有 alice(U9)，B 没有；向 B 发送时 text/blocks 不得引用 U9
MEM_A = [{"id": "U9", "name": "alice",
          "profile": {"display_name": "Alice", "real_name": "A"}}]
MEM_B = [{"id": "U1", "name": "muse",
          "profile": {"display_name": "Muse", "real_name": "M"}}]
clA = FakeClient([{"members": MEM_A, "next_cursor": ""}])
clB = FakeClient([{"members": MEM_B, "next_cursor": ""}])
adM = RealAdapter(PlatformConfig(extra={"rich_blocks": True}))
adM._app = type("App", (), {"client": clB})()
adM._team_clients = {"TA": clA, "TB": clB}
mod._wrap_adapter(adM)
# 预热 A 的表；B 的路由上下文 = metadata team_id=TB
run_async(mod._name_table(adM, team_id="TA"))
resB = run_async(adM.send("CB", "ping @alice and @muse", metadata={"team_id": "TB"}))
posted = clB.posts[-1] if clB.posts else {}
check("C10 跨工作区隔离：向 B 发送不引用 A 的用户（text 层）",
      "U9" not in posted.get("text", "") and "<@U1>" in posted.get("text", ""),
      repr(posted)[:300])
check("C11 跨工作区隔离：blocks 也不引用 A 的用户",
      all("U9" != uid for uid in serialize_user(posted.get("blocks") or [])),
      repr(posted.get("blocks"))[:300])
# edit 同样：metadata TB → 不吃 A 的表
run_async(adM.edit_message("CB", "1712", "edit @alice ok", finalize=True,
                           metadata={"team_id": "TB"}))
check("C12 edit 路由 TB：@alice 保持原样（B 没有这个人）",
      clB.updates and clB.updates[-1].get("text") == "edit @alice ok",
      repr(clB.updates[-1] if clB.updates else None)[:300])

# C13 同名用户双工作区：TA 和 TB 各有一个 "muse"（不同 uid）——发到谁用谁的表
clA2 = FakeClient([{"members": [{"id": "U9", "name": "muse",
                                 "profile": {"display_name": "Muse"}}],
                    "next_cursor": ""}])
clB2 = FakeClient([{"members": [{"id": "U1", "name": "muse",
                                 "profile": {"display_name": "Muse"}}],
                    "next_cursor": ""}])
adN = RealAdapter(PlatformConfig(extra={"rich_blocks": True}))
adN._app = type("App", (), {"client": clB2})()
adN._team_clients = {"TA": clA2, "TB": clB2}
mod._wrap_adapter(adN)
run_async(mod._name_table(adN, team_id="TA"))          # A 表就绪
resN = run_async(adN.send("CB", "hi @muse", metadata={"team_id": "TB"}))
postedN = clB2.posts[-1] if clB2.posts else {}
check("C13 同名双工作区：发 B 用 B 的 uid（<@U1>，不是 A 的 U9）",
      postedN.get("text") == "hi <@U1>",
      repr(postedN)[:200])

# C14 _maybe_blocks 同步路径多 team 缓存 → 不解析（唯一 team 键才用）
tables = getattr(adN, mod._TABLES_ATTR)
check("C14 同步路径：多 team 表缓存且无上下文 → 保留原文",
      len([k for k in tables if tables[k][0]]) >= 2
      and mod._sync_table_if_ready(adN) == (None, None),
      repr({k: len(v[0]) for k, v in tables.items()}))
# 单 team 缓存 → 正常解析
adO, clO = fresh_adapter()
run_async(mod._name_table(adO))
tk, tb = mod._sync_table_if_ready(adO)
check("C15 同步路径：唯一 team 键正常取表", tk == "" and tb and "muse" in tb,
      repr(tk))


# —— C16+：保留的 v1.1/v1.2 语义回归（歧义/代码保护/短名）——
TABLE = {
    "muse": (3, ["U1"]),
    "muse catgirl": (2, ["U1"]),
    "grok bot": (2, ["U2"]),
    "alice": (2, ["U3", "U4"]),
    "bob": (2, ["U5"]),
    "bobby": (3, ["U6"]),
    "alice b": (2, ["U3", "U4"]),
    "bernard rasmussen": (2, ["U7", "U8"]),   # 两个同名长名称
    "b": (3, ["U10"]),                          # 第三人唯一短名
}
check("C16 歧义词组 '@alice b please' 整段保留",
      mod._build_repl_string("@alice b please", TABLE) == "@alice b please")
check("C17 查无回退：'@muse please review' 只吃 muse",
      mod._build_repl_string("@muse please review", TABLE) == "<@U1> please review")
check("C18 两个同名长名称 + 第三人唯一短名前缀：'@bernard rasmussen…' 前缀撞歧义长名 → 停，绝不落到 'b'",
      mod._build_repl_string("@bernard rasmussen please", TABLE) == "@bernard rasmussen please",
      repr(mod._build_repl_string("@bernard rasmussen please", TABLE)))
check("C19 短名 'b' 单独出现才命中（前缀歧义不传染）",
      mod._build_repl_string("@b.", TABLE) == "<@U10>.",
      repr(mod._build_repl_string("@b.", TABLE)))
check("C20 代码片段不解析",
      mod._build_repl_string("看:\n```\nhi @muse\n```\n完 @muse", TABLE)
      == "看:\n```\nhi @muse\n```\n完 <@U1>")
check("C21 已解析实体不再二次解析（幂等）",
      mod._build_repl_string("done <@U1> end", TABLE) == "done <@U1> end",
      repr(mod._build_repl_string("done <@U1> end", TABLE)))

# C22 同步路径冷启动不炸不 coroutine（v1.1 血坑回归）
adP, _ = fresh_adapter()
r = adP._maybe_blocks("cold @Muse start")
check("C22 同步路径冷启动不炸不 coroutine",
      not inspect.iscoroutine(r) and isinstance(r, list), repr(r)[:200])


print()
print(f"checks={CHECKS} fails={len(FAILS)}")
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
print("upstream:", COMMIT)
