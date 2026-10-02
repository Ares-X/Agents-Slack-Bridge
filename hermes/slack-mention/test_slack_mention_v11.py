#!/usr/bin/env python3
"""slack-mention v1.1 自测：五个 review 点逐条断言（不依赖 Slack API）。"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location(
    "slack_mention_v11", "/home/aresx/.hermes/plugins/slack-mention/__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


TABLE = {
    # score: 3=username 2=display/real
    "muse": (3, ["U1"]),
    "muse catgirl": (2, ["U1"]),          # display name 多词
    "grok bot": (2, ["U2"]),
    "alice": (2, ["U3", "U4"]),           # 同可靠度同名 → 歧义
    "bob": (2, ["U5"]),
    "bobby": (3, ["U6"]),                 # username
}

# ---- ① 代码保护 ----
check("① inline code 不解析",
      mod._build_repl_string("ping `@muse` now", TABLE) == "ping `@muse` now",
      repr(mod._build_repl_string("ping `@muse` now", TABLE)))
check("① fenced code 不解析",
      mod._build_repl_string("看:\n```\nhi @muse\n```\n完", TABLE)
      == "看:\n```\nhi @muse\n```\n完")
check("① 代码外正常解析",
      mod._build_repl_string("喂 @muse 看这个 `@bob`", TABLE)
      == "喂 <@U1> 看这个 `@bob`",
      repr(mod._build_repl_string("喂 @muse 看这个 `@bob`", TABLE)))

# 实体→元素：代码节点里绝不插 user 元素
blocks = [{"type": "rich_text", "elements": [
    {"type": "rich_text_section", "elements": [
        {"type": "text", "text": "示例 "},
        {"type": "rich_text_inline_code", "elements": [
            {"type": "text", "text": "x <@U1> y"}]},
        {"type": "text", "text": " 尾巴 <@U2> 完"},
    ]}]}]
out = mod._tokenize_blocks(blocks)[0]["elements"][0]["elements"]
code_child = out[1]["elements"]
tail = out[2:]
check("① 代码节点内实体保持 text 原样",
      code_child == [{"type": "text", "text": "x <@U1> y"}], repr(code_child))
check("① 代码外实体转 user 元素",
      any(e.get("type") == "user" and e.get("user_id") == "U2" for e in tail),
      repr(tail))

# ---- ② 词组回退 ----
check("② '@Muse please review' 只吃 Muse",
      mod._build_repl_string("@Muse please review", TABLE) == "<@U1> please review",
      repr(mod._build_repl_string("@Muse please review", TABLE)))
check("② '@Grok Bot can you review?' 不吞英文",
      mod._build_repl_string("@Grok Bot can you review?", TABLE)
      == "<@U2> can you review?",
      repr(mod._build_repl_string("@Grok Bot can you review?", TABLE)))
check("② '@Muse.' 句号不吃",
      mod._build_repl_string("@Muse.", TABLE) == "<@U1>.",
      repr(mod._build_repl_string("@Muse.", TABLE)))
check("② 未知词组整段保留",
      mod._build_repl_string("@Nobody Here helps", TABLE) == "@Nobody Here helps",
      repr(mod._build_repl_string("@Nobody Here helps", TABLE)))
check("② 前缀否定不吃 'x@muse'",
      mod._build_repl_string("x@muse", TABLE) == "x@muse")

# ---- ③ 歧义 ----
check("③ username 优先于 display 同名",
      mod._lookup(TABLE, "muse") == "U1")
check("③ 同分同名不解析",
      mod._lookup(TABLE, "alice") is None)
check("③ display 多词命中",
      mod._lookup(TABLE, "grok bot") == "U2")

# ---- ④⑤ 包装逻辑（假 adapter 全链路） ----
import asyncio


class FakeClient:
    def __init__(self, members):
        self.members = members
        self.calls = 0

    async def users_list(self, limit=0, cursor=None):
        self.calls += 1
        return {"members": self.members, "response_metadata": {"next_cursor": ""}}


class FakeApp:
    def __init__(self, client):
        self.client = client


class FakeAdapter:
    """最小实现树内被包装方法的语义（只测包装层行为）。"""

    def __init__(self, client):
        self._app = FakeApp(client)
        self._channel_team = {}
        self.seen = []

    def _get_client(self, chat_id, team_id=None):
        return self._app.client

    def _maybe_blocks(self, content):
        # 模拟树内：文本 → 单 section text 元素
        return [{"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [
                {"type": "text", "text": content}]}]}]

    async def _post_chunks(self, chat_id, team_id, content, formatted, thread_ts):
        self.seen.append(("post", chat_id, team_id, content, formatted))
        return {"ts": "1"}

    async def edit_message(self, chat_id, message_id, content, *, finalize=False,
                           metadata=None):
        self.seen.append(("edit", chat_id, content, finalize))
        return True

    async def _seal_stream(self, chat_id, stream, final_text):
        self.seen.append(("seal", chat_id, final_text))
        return True


MEMBERS = [
    {"id": "U1", "name": "muse", "profile": {"display_name": "Muse",
                                              "real_name": "Muse C"}},
    {"id": "U2", "name": "grokbot", "profile": {"display_name": "Grok Bot",
                                                 "real_name": "Grok"}},
    # 另一个人的 display name 也叫 muse → 应被 username 顶掉
    {"id": "U9", "name": "other", "profile": {"display_name": "muse",
                                               "real_name": "Other"}},
]


def run_async(coro):
    return asyncio.run(coro)


ad = FakeAdapter(FakeClient(list(MEMBERS)))
mod._wrap_adapter(ad)

# ④ 冷启动：_maybe_blocks（同步）无表 → 不解析不炸
b = ad._maybe_blocks("hi @Muse please review")
txt = b[0]["elements"][0]["elements"][0]["text"]
check("④ 冷启动同步路径安全跳过", txt == "hi @Muse please review", repr(txt))

# ④ 冷启动流式收尾（_seal_stream async 出口先建表）
run_async(ad._seal_stream("C1", {"ts": "1"}, "done @Grok Bot see?"))
seal = [s for s in ad.seen if s[0] == "seal"][-1]
check("④ 流式收尾冷启动也解析",
      seal[2] == "done <@U2> see?", repr(seal[2]))

# ④ 编辑路径
run_async(ad.edit_message("C1", "1", "edit @Muse.", finalize=True))
edit = [s for s in ad.seen if s[0] == "edit"][-1]
check("④ 编辑路径解析", edit[2] == "edit <@U1>.", repr(edit[2]))

# 正常发送（_post_chunks 出口）
run_async(ad._post_chunks("C1", None, "body @Muse.", "body @Muse.", None))
post = [s for s in ad.seen if s[0] == "post"][-1]
check("④ post 路径解析 content+formatted",
      post[3] == "body <@U1>." and post[4] == "body <@U1>.",
      repr((post[3], post[4])))

# 同步路径在表预热后也解析
b = ad._maybe_blocks("now @Grok Bot ok")
els = b[0]["elements"][0]["elements"]
serialized = "".join(
    "<@%s>" % e["user_id"] if e.get("type") == "user" else e.get("text", "")
    for e in els)
check("④ 表预热后同步路径解析", serialized == "now <@U2> ok", repr(els))

# ③ 深链：display "muse" 被 username 顶掉 → @muse 永远指 U1（不是 U9）
check("③ 表内 username 优先级落地",
      mod._lookup(run_async(mod._name_table(ad)), "muse") == "U1")

# ⑤ 多工作区表隔离
class FakeAdapter2(FakeAdapter):
    pass


ad2 = FakeAdapter2(FakeClient([
    {"id": "U1", "name": "someone", "profile": {"display_name": "Muse"}}]))
ad2._channel_team = {"C1": "T1"}
mod._wrap_adapter(ad2)
t_t1 = run_async(mod._name_table(ad2, chat_id="C1"))
check("⑤ 表按 team 隔离建表",
      mod._TABLES_ATTR in dir(ad2) or hasattr(ad2, mod._TABLES_ATTR))
keys = list(getattr(ad2, mod._TABLES_ATTR).keys())
check("⑤ team 键=T1", keys == ["T1"], repr(keys))
check("⑤ T1 表内容独立",
      mod._lookup(t_t1, "muse") == "U1" and "grok bot" not in t_t1)

print()
if FAILS:
    print("FAILED:", len(FAILS), FAILS)
    sys.exit(1)
print("ALL PASS")
