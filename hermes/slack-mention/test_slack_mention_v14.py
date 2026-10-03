#!/usr/bin/env python3
"""slack-mention v1.4：已部署上游 836b5f8 的流式收尾契约回归。

部署树（~/.hermes/hermes-agent，836b5f8253）的流式契约与 v1.3 测试钉死的
a5e7df27c7 不同：
  - 无 _commit_stream/_stream_key/_stream_relation——文本收尾走
    _try_finalize_stream(chat_id, content[, metadata]) → _seal_stream(chat_id,
    stream, final_text=, blocks=)，流身份是 _active_streams 的 **chat_id 索引**
    （不是 (team, chat, thread) 三元组 key）；
  - _try_finalize_stream 在 send() 顶部就 pop 流并收尾：**先于** _post_chunks，
    r5 的 async 出口（预热建表）在该路径永不触发；其 Block Kit 收尾
    chat_update 直接走 _get_client，**不经过 edit_message 包装**；
  - send_draft 片段切换/前缀断裂时就地 seal（无文本）。

RED 断言（v1.3 r5 插件 + 836 上游必红；v1.4 全绿）：
  T1  冷缓存全链路（send_draft→send 收尾）：stopStream 的未流出尾段里的
      @mention 必须解析成 <@UID>（send 侧收尾不经过 _post_chunks/edit_message，
      冷表必须在收尾前 await 建好）。
  T2  富文本收尾的 chat.update blocks：尾段 @Muse → user 元素 <@U1>；
      已发送前缀 @Grok Bot 字节不动（text 与 blocks 都查）。
  T3  片段切换 seal（draft_id 变）：seal 无文本（纯封口），不炸、不重复解析。
  T4  前缀断裂（draft 重写）：seal 旧流 + 新流重启，旧流尾段不再解析。
  T5  edit_message 普通路径照常解析（不因流收尾直通窗口误伤）。
  T6  幂等：已解析 <@U1> 不被二次改写；mention 表按 team 严格绑定
      （836 的 _active_streams 是 chat 索引——跨 team 同 channel 不存在，
      但建表路由仍必须显式 team 优先，绝不拿 A 表猜 B）。

用法：
  ~/.hermes/hermes-agent/venv/bin/python test_slack_mention_v14.py [repo] [commit]
  默认 repo=~/.hermes/hermes-agent，commit=836b5f8253（部署树真身）。
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.hermes/hermes-agent"))
COMMIT = sys.argv[2] if len(sys.argv) > 2 else "836b5f8253"

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
spec = importlib.util.spec_from_file_location("slack_mention_v14", PLUGIN_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if not (REPO / ".git").exists():
    print("ERROR: hermes-agent repo not found:", REPO)
    sys.exit(1)
if subprocess.run(["git", "-C", str(REPO), "cat-file", "-t", COMMIT],
                  capture_output=True).stdout.strip() != b"commit":
    print(f"ERROR: commit not found locally: {COMMIT} (git -C {REPO} fetch origin)")
    sys.exit(1)

WT = Path(tempfile.mkdtemp(prefix="asb-slack-v14-"))
r = subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                    str(WT), COMMIT], capture_output=True, text=True)
if r.returncode != 0:
    shutil.rmtree(WT, ignore_errors=True)
    print("ERROR: worktree add failed:", r.stderr.strip()[:200])
    sys.exit(1)
sys.path.insert(0, str(WT))

import slack_sdk  # noqa: E402
from slack_sdk.web.slack_response import SlackResponse  # noqa: E402

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.slack.adapter import SlackAdapter as RealAdapter  # noqa: E402


def cleanup():
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(WT)],
                   capture_output=True)


import atexit  # noqa: E402
atexit.register(cleanup)


# A. 部署树（836）契约：与 v1.3 测试钉的 a5e7df27c7 不同——这是本套件的存在理由
sig_seal = inspect.signature(RealAdapter._seal_stream)
sig_fin = inspect.signature(RealAdapter._try_finalize_stream)
check("A1 836 _seal_stream 签名=(chat_id, stream, final_text=None, blocks=None)",
      list(sig_seal.parameters) == ["self", "chat_id", "stream", "final_text", "blocks"]
      and sig_seal.parameters["final_text"].default is None
      and sig_seal.parameters["blocks"].default is None,
      str(sig_seal))
check("A2 836 _try_finalize_stream 签名=(chat_id, content[, metadata])",
      list(sig_fin.parameters) in (
          ["self", "chat_id", "content"], ["self", "chat_id", "content", "metadata"]),
      str(sig_fin))
check("A3 836 无 _commit_stream/_stream_key/_stream_relation（v1.3 包装的老树档）",
      not any(hasattr(RealAdapter, n) for n in
              ("_commit_stream", "_stream_key", "_stream_relation")),
      str([n for n in ("_commit_stream", "_stream_key", "_stream_relation")
           if hasattr(RealAdapter, n)]))
src_send = inspect.getsource(RealAdapter.send)
check("A4 836 send() 顶部先 _try_finalize_stream 再 _post_chunks（收尾不经预热出口）",
      "_try_finalize_stream" in src_send and "_post_chunks" in src_send
      and src_send.index("_try_finalize_stream") < src_send.index("_post_chunks"))
src_fin = inspect.getsource(RealAdapter._try_finalize_stream)
check("A5 836 收尾 chat_update 直走 _get_client（不经 self.edit_message）",
      "_maybe_blocks" in src_fin and "chat_update" in src_fin
      and "self.edit_message" not in src_fin)


# B. FakeClient：真实 SlackResponse 形状
class FakeClient:
    def __init__(self, pages, team=None):
        self.pages = pages
        self.team = team
        self.users_list_calls = 0
        self.posts, self.updates, self.stops, self.appends, self.starts = [], [], [], [], []

    async def users_list(self, limit=0, cursor=None):
        self.users_list_calls += 1
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
        self.appends.append(kw)
        return SlackResponse(client=None, http_verb="POST", api_url="chat.appendStream",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True})

    async def conversations_open(self, users=None):
        return SlackResponse(client=None, http_verb="POST", api_url="conversations.open",
                             req_args={}, headers={}, status_code=200,
                             data={"ok": True, "channel": {"id": "D1"}})


PAGE = [{"id": "U1", "name": "muse",
         "profile": {"display_name": "Muse", "real_name": "M"}},
        {"id": "U2", "name": "grokbot",
         "profile": {"display_name": "Grok Bot", "real_name": "G"}}]


def make_adapter(pages=None, team=None, rich=True):
    ad = RealAdapter(PlatformConfig(extra={"rich_blocks": rich}))
    client = FakeClient(pages or [{"members": PAGE, "next_cursor": ""}])
    ad._app = type("App", (), {"client": client})()
    ad._team_clients = {team: client} if team else {}
    return ad, client


def fresh(pages=None, team="T1", rich=True):
    a, c = make_adapter(pages, team, rich)
    mod._wrap_adapter(a)
    return a, c


def serialize_user(blocks):
    found = set()
    stack = list(blocks or [])
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "user":
                found.add(node.get("user_id"))
            stack.extend(node.get("elements") or [])
    return found


# T1 冷缓存全链路：draft 流式 → send 收尾，尾段 mention 解析进 stopStream delta
ad1, cl1 = fresh()
md1 = {"team_id": "T1", "thread_id": "1000"}
d1 = run_async(ad1.send_draft("C1", 1, "done @Grok Bot se", metadata=md1))
s1 = run_async(ad1.send("C1", "done @Grok Bot se… and @Muse.", metadata=md1))
tail1 = [k.get("markdown_text", "") for k in cl1.stops]
check("T1 冷缓存收尾：未流出尾段 @Muse → <@U1>（send 侧 seal 不经预热出口）",
      getattr(d1, "success", False) and getattr(s1, "success", False)
      and any("… and <@U1>." in t for t in tail1),
      repr((d1, s1, cl1.stops))[:400])

# T2 富文本收尾：finalize chat.update 的 blocks 尾段解析，前缀字节不动
ad2, cl2 = fresh(team="T2")
md2 = {"team_id": "T2", "thread_id": "2000"}
run_async(ad2.send_draft("C2", 1, "- answer @Grok Bot partial", metadata=md2))
run_async(ad2.send("C2", "- answer @Grok Bot partial\n- tag @Muse too", metadata=md2))
u2 = list(cl2.updates)
check("T2 富文本收尾：尾段 @Muse → <@U1>、前缀 @Grok Bot 原样（text+blocks）",
      u2 and all(
          "answer @Grok Bot partial" in u.get("text", "")
          and "tag <@U1> too" in u.get("text", "")
          and "U1" in serialize_user(u.get("blocks"))
          and "U2" not in serialize_user(u.get("blocks"))
          for u in u2),
      repr(u2)[:400])

# T3 片段切换：seal 无文本（纯封口），不炸
ad3, cl3 = fresh(team="T3")
md3 = {"team_id": "T3", "thread_id": "3000"}
r3a = run_async(ad3.send_draft("C3", 1, "seg one @Muse", metadata=md3))
r3b = run_async(ad3.send_draft("C3", 2, "seg two", metadata=md3))
check("T3 片段切换：旧流纯封口（无 markdown_text）、新流开启",
      getattr(r3a, "success", False) and getattr(r3b, "success", False)
      and cl3.stops and all("markdown_text" not in k for k in cl3.stops)
      and len(cl3.starts) == 2,
      repr((r3a, r3b, cl3.stops, cl3.starts))[:400])

# T4 前缀断裂：draft 重写 → seal + fail 帧；随后 send 不误收尾
ad4, cl4 = fresh(team="T4")
md4 = {"team_id": "T4", "thread_id": "4000"}
run_async(ad4.send_draft("C4", 1, "draft @Grok Bot", metadata=md4))
r4 = run_async(ad4.send_draft("C4", 1, "REWRITTEN no mention", metadata=md4))
check("T4 前缀断裂：旧流 seal、帧失败回退 edit 路径",
      (not getattr(r4, "success", True)) and cl4.stops,
      repr((r4, cl4.stops))[:300])

# T5 edit_message 普通路径照常解析（不被流收尾直通窗口误伤；冷表由 async 出口自建）
ad5, cl5 = fresh(team="T5")
e5 = run_async(ad5.edit_message("C5", "1712", "edit @Muse ok", finalize=True,
                                metadata={"team_id": "T5"}))
check("T5 edit 普通路径：@Muse → <@U1> 照常解析",
      getattr(e5, "success", False) and cl5.updates
      and "edit <@U1> ok" == cl5.updates[-1].get("text", ""),
      repr((e5, cl5.updates[-1] if cl5.updates else None))[:300])

# T6 幂等 + team 严格绑定：已解析实体不二次改写；B 发送不吃 A 表
ad6, cl6 = fresh(team="T6")
s6 = run_async(ad6.send("C6", "done <@U1> end", metadata={"team_id": "T6"}))
check("T6a 幂等：已解析 <@U1> 不被改写",
      getattr(s6, "success", False) and "<@U1>" in cl6.posts[-1].get("text", "")
      and "<<" not in cl6.posts[-1].get("text", ""),
      repr(cl6.posts[-1] if cl6.posts else None)[:200])

clA = FakeClient([{"members": [{"id": "U9", "name": "alice",
                                "profile": {"display_name": "Alice"}}],
                   "next_cursor": ""}])
adM = RealAdapter(PlatformConfig(extra={"rich_blocks": True}))
adM._app = type("App", (), {"client": FakeClient([{"members": [], "next_cursor": ""}])})()
adM._team_clients = {"TA": clA, "TB": FakeClient([{"members": PAGE, "next_cursor": ""}])}
mod._wrap_adapter(adM)
run_async(mod._name_table(adM, team_id="TA"))
resB = run_async(adM.send("CB", "ping @alice and @muse", metadata={"team_id": "TB"}))
postedB = (adM._team_clients["TB"].posts or [{}])[-1]
check("T6b team 严格绑定：向 B 发送不引用 A 的 U9、@muse 用 B 的 <@U1>",
      "U9" not in postedB.get("text", "") and "<@U1>" in postedB.get("text", ""),
      repr(postedB)[:300])

# T7 真正文末尾也可能是游标字形；orig 只能剥掉原来那一枚。
# 第二项同时保护 claim：重复剥除会让正文短于 sent，另发整篇且遗留旧流。
for index, (draft, final, expected) in enumerate([
    ("Wait", "Wait……", "Wait…"),
    ("Wait…▌", "Wait……", "Wait…"),
    ("Wait", "Wait… ▌  ", "Wait…"),
    ("Wait", "Wait  ▌\n", "Wait"),
    ("Wait", "Wait  ", "Wait  "),
    ("Wait", "Wait @Muse …\n", "Wait <@U1>"),
]):
    ad, client = fresh()
    metadata = {"team_id": "T1", "thread_id": "7000"}
    run_async(ad.send_draft("C7", 1, draft, metadata=metadata))
    sent = ad._active_streams["C7"]["sent"]
    result = run_async(ad.send("C7", final, metadata=metadata))
    check(f"T7.{index} 原游标只剥一次，单流正确封口",
          getattr(result, "success", False)
          and len(client.starts) == len(client.stops) == 1
          and client.stops[0].get("markdown_text", "") == expected[len(sent):]
          and [u.get("text") for u in client.updates] == [expected]
          and not client.posts and not ad._active_streams,
          repr((client.stops, client.updates, client.posts))[:400])

print()
print(f"checks={CHECKS} fails={len(FAILS)}")
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
print("upstream:", COMMIT)
