#!/usr/bin/env python3
"""slack-mention v1.2 测试：真实上游契约 + 从当前 checkout 导入插件。

三个真实来源（不复制实现）：
  A. 树内真源码做静态契约校验 —— inspect 真实 SlackAdapter 的方法签名：
     edit_message / _post_chunks / _seal_stream / _maybe_blocks / _get_client；
     且 _metadata_team_id 认的 metadata keys 与插件路由表一致。
  B. 真实 block_kit.render_blocks —— 「代码保护识别真实 renderer 的 style.code」
     直接对真 renderer 的产物断言，不是手搓的假形状。
  C. 真实 SlackAdapter 实例 —— 插件包装跑在真 adapter 对象上，edit/_seal_stream
     走真方法体（FakeClient 只垫最底层的 Slack SDK 调用）。

用法：
  cd ~/workspace/asb/hermes
  HERMES_ROOT=~/.hermes/hermes-agent python3 slack-mention/test_slack_mention_v12.py
"""
import asyncio
import importlib.util
import inspect
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HERMES_ROOT = os.environ.get(
    "HERMES_ROOT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, HERMES_ROOT)
# 注意：绝不把 plugins/platforms 本身放进 sys.path——它底下的 email/、irc/ 等
# 目录会遮蔽同名标准库（email.utils 就是这么炸的）。插件包一律经
# plugins.platforms.slack 的完整包路径导入。

FAILS = []


def check(name, cond, detail=""):
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
spec = importlib.util.spec_from_file_location("slack_mention_v12", PLUGIN_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# ---------------------------------------------------------------------------
# A. 真实上游契约（inspect 树内 SlackAdapter，非复制）
# ---------------------------------------------------------------------------
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.slack import adapter as slack_adapter_mod  # noqa: E402

SlackAdapter = slack_adapter_mod.SlackAdapter
sig_edit = inspect.signature(SlackAdapter.edit_message)
sig_post = inspect.signature(SlackAdapter._post_chunks)
sig_seal = inspect.signature(SlackAdapter._seal_stream)
sig_blocks = inspect.signature(SlackAdapter._maybe_blocks)
sig_getclient = inspect.signature(SlackAdapter._get_client)

check("A1 edit_message kwargs 面=(chat_id,message_id,content,finalize,metadata)",
      list(sig_edit.parameters) == ["self", "chat_id", "message_id", "content",
                                    "finalize", "metadata"],
      str(sig_edit))
check("A2 _post_chunks kwargs 面=(chat_id,team_id,content,formatted,thread_ts)",
      list(sig_post.parameters) == ["self", "chat_id", "team_id", "content",
                                    "formatted", "thread_ts"],
      str(sig_post))
check("A3 _seal_stream 真实契约=(chat_id,stream,final_text=None,blocks=None)",
      list(sig_seal.parameters) == ["self", "chat_id", "stream", "final_text",
                                    "blocks"]
      and sig_seal.parameters["final_text"].default is None
      and sig_seal.parameters["blocks"].default is None,
      str(sig_seal))
check("A4 _maybe_blocks 同步 def（非 coroutine function）",
      not inspect.iscoroutinefunction(SlackAdapter._maybe_blocks)
      and list(sig_blocks.parameters) == ["self", "content"],
      str(sig_blocks))
check("A5 _get_client 认显式 team_id 参数",
      list(sig_getclient.parameters) == ["self", "chat_id", "team_id"],
      str(sig_getclient))

# _seal_stream 的 delta 追加语义：final_text 是完整终稿、只发 final_text[len(sent):]
src_seal = inspect.getsource(SlackAdapter._seal_stream)
check("A6 上游 _seal_stream 源码即 delta 语义（final_text[len(sent):]）",
      "final_text[len(sent)" in src_seal and "startswith" in src_seal)

# 插件 metadata 路由 keys ⊆ 树内 _metadata_team_id 认的 keys
src_meta = inspect.getsource(SlackAdapter._metadata_team_id)
for k in mod._METADATA_TEAM_KEYS:
    if f'"{k}"' not in src_meta:
        check(f"A7 metadata key {k!r} 不在上游 _metadata_team_id", False)
        break
else:
    check("A7 插件 team 路由 keys 与上游 _metadata_team_id 同源", True)

# ---------------------------------------------------------------------------
# B. 真实 renderer：代码保护的 style.code 识别
#    注：段落走 section/mrkdwn（实体在 mrkdwn 里已是原生语法）；style.code 的
#    text 元素出现在 rich_text 结构（列表/引用/表格）里——用列表语境逼真产物。
# ---------------------------------------------------------------------------
from plugins.platforms.slack.block_kit import render_blocks  # noqa: E402

blocks = render_blocks("- item `code <@U1> x` tail\n- two <@U2> plain")
flat_b = [e for b in blocks if b.get("type") == "rich_text"
          for top in b.get("elements", [])
          for sec in top.get("elements", [])
          for e in sec.get("elements", [])]
check("B1 真实 renderer 的 inline code 带 style.code",
      any((e.get("style") or {}).get("code") and "<@U1>" in e.get("text", "")
          for e in flat_b),
      repr(blocks))

# 真实产物过插件后处理：代码内实体绝不转 user 元素，代码外转
out = mod._tokenize_blocks(blocks)
flat = [e for b in out if b.get("type") == "rich_text"
        for top in b.get("elements", [])
        for sec in top.get("elements", [])
        for e in sec.get("elements", [])]
code_els = [e for e in flat if (e.get("style") or {}).get("code")]
check("B2 style.code 节点内的 <@U1> 保持 text 原样",
      code_els and all(
          e.get("type") == "text" and "<@U1>" in e.get("text", "")
          for e in code_els),
      repr(code_els))
check("B3 代码节点内没有 user 元素",
      not any(e.get("type") == "user" and e.get("user_id") == "U1" for e in flat),
      repr(flat))

# fenced code（rich_text_preformatted，顶层围栏）同样不插 user 元素
blocks2 = render_blocks("文:\n```\nhi <@U2> bye\n```\n完")
out2 = mod._tokenize_blocks(blocks2)
pre_texts = [e.get("text", "") for b in out2 if b.get("type") == "rich_text"
             for top in b.get("elements", [])
             if top.get("type") == "rich_text_preformatted"
             for e in top.get("elements", [])]
check("B4 preformatted 内的实体保持 text",
      any("<@U2>" in t for t in pre_texts)
      and not any(e.get("type") == "user" and e.get("user_id") == "U2"
                  for b in out2 if b.get("type") == "rich_text"
                  for top in b.get("elements", [])
                  if top.get("type") == "rich_text_preformatted"
                  for e in top.get("elements", [])),
      repr(out2))
check("B5 代码外实体转 user 元素（同一渲染的 rich_text 语境）",
      any(e.get("type") == "user" and e.get("user_id") == "U2" for e in flat),
      repr(flat))

# 手搓兜底断言：纯 style.code 形状（万一 renderer 改版）也认
hand = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
    {"type": "text", "text": "code <@U9> inner"},
    {"type": "text", "text": "outside <@U8> ok"}]}]}]
# 手搓第二节点模拟 style.code
hand[0]["elements"][0]["elements"][0]["style"] = {"code": True}
outh = mod._tokenize_blocks(hand)
els = outh[0]["elements"][0]["elements"]
check("B6 手搓 style.code 节点也保护 / 非代码节点转 user",
      any(e.get("type") == "text" and "<@U9>" in e.get("text", "")
          and (e.get("style") or {}).get("code") for e in els)
      and any(e.get("type") == "user" and e.get("user_id") == "U8" for e in els),
      repr(els))

# ---------------------------------------------------------------------------
# 名字解析：查无 vs 歧义（文本层）
# ---------------------------------------------------------------------------
TABLE = {
    "muse": (3, ["U1"]),
    "muse catgirl": (2, ["U1"]),
    "grok bot": (2, ["U2"]),
    "alice": (2, ["U3", "U4"]),   # 同分同名 → 歧义
    "bob": (2, ["U5"]),
    "bobby": (3, ["U6"]),
    "alice b": (2, ["U3", "U4"]),  # 歧义传染到词组
}
check("C1 查无返回 nomatch", mod._lookup(TABLE, "nobody") == (None, "nomatch"))
check("C2 歧义返回 ambiguous", mod._lookup(TABLE, "alice") == (None, "ambiguous"))
check("C3 username 唯一直取", mod._lookup(TABLE, "muse") == ("U1", ""))

check("C4 歧义不回退吃短名：'@alice please' 整段保留",
      mod._build_repl_string("@alice please", TABLE) == "@alice please",
      repr(mod._build_repl_string("@alice please", TABLE)))
check("C5 歧义词组同防：'@alice b please' 保留",
      mod._build_repl_string("@alice b please", TABLE) == "@alice b please",
      repr(mod._build_repl_string("@alice b please", TABLE)))
check("C6 查无可回退：'@muse please review' 只吃 muse",
      mod._build_repl_string("@muse please review", TABLE) == "<@U1> please review",
      repr(mod._build_repl_string("@muse please review", TABLE)))
check("C7 歧义+句号不吃标点",
      mod._build_repl_string("@alice.", TABLE) == "@alice.",
      repr(mod._build_repl_string("@alice.", TABLE)))
check("C8 正常名字+句号",
      mod._build_repl_string("@Muse.", TABLE) == "<@U1>.",
      repr(mod._build_repl_string("@Muse.", TABLE)))
check("C9 短名回退保留（v1.1）",
      mod._build_repl_string("@Grok Bot can you review?", TABLE)
      == "<@U2> can you review?")
check("C10 代码片段不解析（v1.1，文本层）",
      mod._build_repl_string("看:\n```\nhi @muse\n```\n完 @muse", TABLE)
      == "看:\n```\nhi @muse\n```\n完 <@U1>")

# ---------------------------------------------------------------------------
# C+. 真实 SlackAdapter 实例上的包装（FakeClient 只垫 SDK 层）
# ---------------------------------------------------------------------------
from plugins.platforms.slack.adapter import SlackAdapter as RealAdapter  # noqa: E402


class FakeClient:
    def __init__(self, members, team=None):
        self.members = members
        self.team = team
        self.users_list_calls = 0

    async def users_list(self, limit=0, cursor=None):
        self.users_list_calls += 1
        return {"members": self.members,
                "response_metadata": {"next_cursor": ""}}

    async def chat_stopStream(self, **kw):
        self.last_stop = kw
        return {"ok": True}

    async def chat_update(self, **kw):
        self.last_update = kw
        return {"ok": True}

    async def chat_postMessage(self, **kw):
        self.last_post = kw
        return {"ok": True, "ts": "1712"}


def make_adapter(members, team_clients=None):
    ad = RealAdapter(PlatformConfig(extra={"rich_blocks": True}))
    client = FakeClient(members)
    ad._app = type("App", (), {"client": client})()
    ad._team_clients = team_clients or {}
    return ad, client


MEMBERS = [
    {"id": "U1", "name": "muse",
     "profile": {"display_name": "Muse", "real_name": "Muse C"}},
    {"id": "U2", "name": "grokbot",
     "profile": {"display_name": "Grok Bot", "real_name": "Grok"}},
    {"id": "U3", "name": "carol", "profile": {"display_name": "alice"}},
    {"id": "U4", "name": "dave", "profile": {"display_name": "alice"}},
]

ad, client = make_adapter(list(MEMBERS))
mod._wrap_adapter(ad)

# ① _maybe_blocks：同步路径（真实渲染管线，真实 renderer 的 style.code 产物）
# 先经 async 出口预热表（正常会话即此顺序：首条消息走 async send 路径）
run_async(mod._name_table(ad))
b = ad._maybe_blocks("- run `cmd @muse` now\n- ping @Grok Bot done")
flat = [e for blk in b if blk.get("type") == "rich_text"
        for top in blk.get("elements", [])
        for sec in top.get("elements", [])
        for e in sec.get("elements", [])]
code_txt = "".join(e.get("text", "") for e in flat
                   if (e.get("style") or {}).get("code"))
ser = "".join(
    "<@%s>" % e["user_id"] if e.get("type") == "user" else e.get("text", "")
    for e in flat if not (e.get("style") or {}).get("code"))
check("D1 真实 adapter._maybe_blocks：代码内不解析、代码外成 user 元素",
      "cmd @muse" in code_txt and "<@U2>" in ser and "@Grok Bot" not in ser,
      repr(flat))

# ② _seal_stream：真实契约（4 参 + delta 追加语义），冷启动建表
# mention 必须在**未流出尾部**（sent 之后的增量里）→ 解析进收尾增量；
# 已流入 sent 的 mention 无法追溯解析（append-only 物理限制）。
stream = {"ts": "1712", "draft_id": "d1", "sent": "done @Grok Bot se", "started": 0}
ok = run_async(ad._seal_stream("C1", stream, final_text="done @Grok Bot se… and @Muse."))
sent_kw = client.last_stop
check("D2 seal 冷启动建表且解析未流出尾部的 mention",
      ok is True and sent_kw.get("markdown_text") == "… and <@U1>.",
      repr(sent_kw))
check("D3 seal delta 语义=final_text[len(sent):]（只发增量，前缀原样）",
      sent_kw.get("markdown_text", "").startswith("… and "),
      repr(sent_kw.get("markdown_text")))
# mention 恰好骑在 sent 边界上：未流出部分不构成完整候选 → 尾部原样（不炸不丢）
stream_b = {"ts": "1713", "draft_id": "d2", "sent": "done @Gro", "started": 0}
run_async(ad._seal_stream("C1", stream_b, final_text="done @Grok Bot end"))
check("D3b mention 跨 sent 边界：尾部原样保留（不丢字）",
      client.last_stop.get("markdown_text") == "k Bot end",
      repr(client.last_stop))
# 上游记账不动：stream["sent"] 保持原值（上游靠它与 final_text 差值算增量，
# 解析后的终稿 = sent + 已解析增量，前缀关系保持成立）
check("D4 seal 后 stream 记账不动（append-only 口径）",
      stream.get("sent") == "done @Grok Bot se", repr(stream.get("sent")))
# final_text=None → 原样透传（上游契约：不带正文就不碰）
ok_none = run_async(ad._seal_stream("C2", {"ts": "9", "sent": "x"}))
check("D5 final_text=None 原样透传", ok_none is True and "markdown_text" not in client.last_stop,
      repr(client.last_stop))

# ③ edit_message：真实方法体 + metadata 工作区路由
ad2, client2 = make_adapter(list(MEMBERS))
tc = FakeClient(list(MEMBERS))
ad2._team_clients = {"T1": tc}
mod._wrap_adapter(ad2)
run_async(ad2.edit_message("C1", "1712", "edit @Muse done", finalize=True,
                           metadata={"team_id": "T1"}))
check("D6 edit 解析且走 metadata 路由的 client（T1 收到 chat.update）",
      getattr(client2, "last_update", None) is None
      and tc.last_update.get("text") == "edit <@U1> done",
      repr((getattr(client2, "last_update", None), getattr(tc, "last_update", None))))
check("D7 edit 表键=T1（不是 fallback primary）",
      list(getattr(ad2, mod._TABLES_ATTR).keys()) == ["T1"],
      repr(list(getattr(ad2, mod._TABLES_ATTR).keys())))

# ④ 成员表严格绑 team：chat 映射到未认证 team → 空表宁可不解析，绝不 fallback
ad3, client3 = make_adapter(list(MEMBERS))
ad3._channel_team = {"C9": "T9"}          # C9 属 T9，但 T9 未认证（不在 _team_clients）
ad3._team_clients = {}                    # 清空：primary 也不可用
mod._wrap_adapter(ad3)
run_async(ad3._seal_stream("C9", {"ts": "1", "sent": ""}, final_text="hi @Muse"))
kw3 = client3.last_stop
check("D8 team 未认证 → 宁可不解析（不指错人）",
      kw3.get("markdown_text") == "hi @Muse" and client3.users_list_calls == 0,
      repr(kw3))

# ⑤ 多 team 隔离：两个 team 各自建表，互不串
ad4, _ = make_adapter([
    {"id": "U1", "name": "someone", "profile": {"display_name": "Muse"}}])
t_t1_client = FakeClient([
    {"id": "U7", "name": "zz", "profile": {"display_name": "Grok Bot"}}])
ad4._team_clients = {"T1": t_t1_client}
mod._wrap_adapter(ad4)
t_t1 = run_async(mod._name_table(ad4, team_id="T1"))
t_prim = run_async(mod._name_table(ad4, chat_id="C0"))
check("D9 表按 team 隔离（T1 里 Grok Bot=U7，primary 里不存在）",
      mod._lookup(t_t1, "grok bot") == ("U7", "")
      and mod._lookup(t_prim, "grok bot") == (None, "nomatch"),
      repr((mod._lookup(t_t1, "grok bot"), mod._lookup(t_prim, "grok bot"))))
check("D10 建表 client 各归各 team",
      t_t1_client.users_list_calls == 1 and ad4._app.client.users_list_calls == 1,
      repr((t_t1_client.users_list_calls, ad4._app.client.users_list_calls)))

# ⑥ _post_chunks：兜底路径预热 + 解析（真实方法体发真 payload）
ad5, client5 = make_adapter(list(MEMBERS))
mod._wrap_adapter(ad5)
run_async(ad5._post_chunks("C1", None, "body @Muse.", "body @Muse.", None))
check("D11 post 路径 content/formatted 都解析",
      client5.last_post.get("text") == "body <@U1>."
      and client5.last_post.get("blocks"),
      repr(client5.last_post))

# ⑦ 同步路径冷启动安全（v1.1 血坑回归：绝不返回 coroutine）
ad6, _ = make_adapter(list(MEMBERS))
mod._wrap_adapter(ad6)
r = ad6._maybe_blocks("cold @Muse start")
check("D12 同步路径冷启动不炸不 coroutine",
      not inspect.iscoroutine(r) and isinstance(r, list),
      repr(r))

# ⑧ 真实 username 顶掉 display 同名（可靠度）
ad7, _ = make_adapter(list(MEMBERS) + [
    {"id": "U9", "name": "other", "profile": {"display_name": "muse"}}])
mod._wrap_adapter(ad7)
t7 = run_async(mod._name_table(ad7))
check("D13 username 可靠度>display 同名", mod._lookup(t7, "muse") == ("U1", ""))
# 真 display 歧义（carol/dave 都叫 alice）→ 不解析
check("D14 真数据歧义不解析", mod._lookup(t7, "alice") == (None, "ambiguous"))

print()
if FAILS:
    print("FAILED:", len(FAILS), FAILS)
    sys.exit(1)
print("ALL PASS")
