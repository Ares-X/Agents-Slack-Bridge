#!/usr/bin/env python3
"""Slack mention 插件 v1.2（2026-10-03，主人 review 2ef6547 的五项复核意见）

官方面（不变）：
- ``ctx.register_platform_handler("slack", factory)`` — SlackAdapter.connect() 时把
  ``(AsyncApp, adapter)`` 递给 factory，做**实例级**包装（不碰类、不碰树内文件）：
  1. ``adapter._maybe_blocks`` — name→ID 解析后再渲染 rich_text（同步签名，10-03
     凌晨血坑：async 化会被同步调用点拿到 coroutine）。
  2. ``adapter._post_chunks`` — 兜底路径预热成员表。
  3. ``adapter.edit_message`` — 编辑路径同样解析（v1.0 漏）。
  4. ``adapter._seal_stream`` — 原生流收尾同样解析（v1.0 漏）。

v1.2 修的五件事（对齐树内真实契约，2026-10-03 二轮 review）：
① 代码保护识别**真实 renderer** 的 style.code：block_kit.py 渲 inline code 时产出
   ``{"type": "text", "style": {"code": true}}``（不是 rich_text_inline_code 元素
   类型）；v1.1 只认元素类型 → 真实渲染产物漏保护。现在两者都认：元素类型
   （rich_text_preformatted / rich_text_inline_code）或 text 元素带 style.code。
② 「查无名称」与「名称歧义」分开：_lookup 返回
   (uid, "") / (None, "ambiguous") / (None, "nomatch")。
   @name 解析回退链（多词→截短）上遇到歧义**立即停止缩短**——两个 display 都叫
   "alice" 的 workspace 里，"@alice please" 绝不许回退吃掉 please 去撞别的名字。
③ 成员表严格绑定目标 team：上游 _get_client/_client_for 的 fallback 链（map 未知
   → 单工作区兜底 → primary）只用于「发消息」；**建表固定用显式 team_id**（非
   fallback），跨 team 复用 = 拿 A 工作区的名字解析 B 工作区的消息 = 指错人。
④ ``_seal_stream`` 真实契约 = (chat_id, stream, final_text=None, blocks=None)，
   且上游只把 final_text 当**完整终稿**、只发 ``final_text[len(sent):]`` 增量
   （append-only，且仅当 final_text.startswith(sent)）。所以解析只能施加于
   **尚未流出的尾部**：new_final = sent + resolve(final_text[len(sent):])，
   绝不解析整篇再传——否则 startswith 断裂，上游一个字增量都不发（丢内容）；
   已流入 stream 的 mention 无法追溯解析（append-only 的物理限制，接受）。
   stream["sent"] 不动：上游正是靠它与 final_text 的差值算增量（见
   _seal_stream_patched）。
⑤ edit_message 的 kwargs 面保持 (chat_id, message_id, content, finalize=False,
   metadata=None) 原样透传，**且成员表键走 metadata 工作区路由**
   （scope_id/slack_team_id/team_id/team/guild_id/workspace_id，与树内
   _metadata_team_id 同源 keys），不再依赖 _channel_team fallback。

v1.1 保留的既有修复（未回退）：短名回退（"@Muse please review"→只吃 Muse）、
句末标点（"@Muse."→<@U1>.）、围栏/内联代码片段的文本级保护。
"""

import asyncio
import logging
import re
import time

LOG = logging.getLogger("slack.mention_fix")

_RESERVED = {"here", "channel", "everyone", "all"}
_TABLE_TTL = 600.0

_WRAP_ATTR = "_mention_wrap_done"
_TABLES_ATTR = "_mention_team_tables"  # {team_id|"": (table, mono_ts)}

# <@U…> / <#C…> / <!subteam^S…> / <!here|<!channel|<!everyone>
_ENTITY_RE = re.compile(
    r"<(?:(@[A-Z0-9]+)|(#[A-Z0-9]+)|(!subteam\^([A-Z0-9]+))|(!(here|channel|everyone)))>"
)

# ---------------------------------------------------------------------------
# 代码切片：mention 解析绝不碰 fenced code / inline code span 的内容
# 原样保留分隔串，解析后原位拼回（保序，不重排）。
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def _split_code(text):
    """→ [(fragment, is_code)]，片段拼接还原原文。"""
    out, pos = [], 0
    for m in _FENCE_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [(text, False)]


# mention 扫描：@ + 最大跨度（单词(+空格+单词)*，token 可含 .-_）。
# 不靠正则贪婪回退——显式从最长候选逐级截短查表，解决两类洞：
#   "@Muse please review" → 逐级退到 "muse" 命中，英文留在原文；
#   "@Muse." → 候选 "muse." miss 后剥尾部句号 "muse" 命中，句号留在原文。
_TOKEN = r"[A-Za-z0-9_.\-]+"
_MENTION_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9_.@>\-])@(" + _TOKEN + r"(?:[ ]+" + _TOKEN + r")*)"
)


def _lookup(table, name):
    """名字 → 用户 ID。返回 ``(uid, "")`` / ``(None, "ambiguous")`` / ``(None, "nomatch")``。

    可靠度：exact username(3) > exact display/real/normalized(2)。
    高可靠条目直接顶掉低可靠同名；同最高分 >1 个 → ambiguous（宁可不解析，
    绝不指错人）。表里没有这个键 → nomatch（回退链可以继续截短）。
    """
    if name not in table:
        return None, "nomatch"
    score, uids = table[name]
    if score == 3:
        return uids[0], ""  # username 全 workspace 唯一
    if len(uids) == 1:
        return uids[0], ""
    return None, "ambiguous"


def _resolve_fragment(fragment, table):
    """单个非代码片段：@name → <@U…>；解析不了保持原样。"""
    if not table or "@" not in fragment:
        return fragment

    def repl(m):
        span = m.group(0)
        tokens = m.group(1).split(" ")
        # 从最长前缀候选逐级截短；每个候选先原样查，miss 再剥尾部句号查。
        # 查无(nomatch) → 继续缩短找更短的名字；
        # 歧义(ambiguous) → 这个名字有两个主人，绝不能回退去吃更短的别的名字，
        # 也绝不指错人 → 整段 @… 原样保留，立刻停。
        for k in range(len(tokens), 0, -1):
            cand_raw = " ".join(tokens[:k])
            for cand in (cand_raw, cand_raw.rstrip(".")):
                if not cand:
                    continue
                low = cand.lower()
                if low in _RESERVED:
                    return span  # @here/@channel 等保留字，整段原样
                uid, why = _lookup(table, low)
                if uid:
                    # 没被候选吃掉的尾巴（句号/被截短的词）留在原文
                    return "<@%s>%s" % (uid, span[1 + len(cand):])
                if why == "ambiguous":
                    return span
        return span

    return _MENTION_SPAN_RE.sub(repl, fragment)


def _build_repl_string(text, table):
    """对整段文本（可能含代码）做 mention 解析，代码片段原样保留。"""
    if not table or "@" not in text:
        return text
    return "".join(
        frag if is_code else _resolve_fragment(frag, table)
        for frag, is_code in _split_code(text)
    )


# ---------------------------------------------------------------------------
# 实体 → 原生 rich_text 元素（发出去侧）
# ---------------------------------------------------------------------------

# text 元素 style 里可安全继承给 user/channel 等原生元素的键（Slack schema 认可
# 的 rich_text text style 子集；其余键塞进 user 元素会被整条降级）。
_INHERITABLE_STYLES = ("bold", "italic", "strike", "code")


def _entity_to_element(m, style=None):
    """实体匹配 → 原生 rich_text 元素；不匹配返回 None。

    Slack schema 不带 sigil：user_id="U…"、channel_id="C…"（<@U…> 的 @ 剥掉）。
    """
    if m.group(1):
        el = {"type": "user", "user_id": m.group(1).lstrip("@")}
    elif m.group(2):
        el = {"type": "channel", "channel_id": m.group(2).lstrip("#")}
    elif m.group(3):
        el = {"type": "usergroup", "usergroup_id": m.group(4)}
    elif m.group(5):
        el = {"type": "broadcast", "range": m.group(6)}
    else:
        return None
    if el is not None and style:
        el["style"] = dict(style)
    return el


def _split_text_element(el):
    """text 元素含实体 → [text…, user/channel 元素…] 列表；无实体返回 None。

    继承周围 runs 的 style（bold/italic/strike/code）到原生元素。
    """
    text = el.get("text") or ""
    hits = list(_ENTITY_RE.finditer(text))
    if not hits:
        return None
    style = el.get("style") or {}
    inheritable = {k: style[k] for k in _INHERITABLE_STYLES if style.get(k)}
    out, pos = [], 0
    for m in hits:
        if m.start() > pos:
            out.append({"type": "text", "text": text[pos:m.start()],
                        **({"style": dict(style)} if style else {})})
        native = _entity_to_element(m, inheritable)
        if native is not None:
            out.append(native)
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": text[pos:],
                    **({"style": dict(style)} if style else {})})
    return [e for e in out if e.get("type") != "text" or e.get("text")]


def _is_code_node(node):
    """真实 renderer 的代码识别：元素类型**或** text 元素的 style.code。

    block_kit.py 的 inline code 产出 {"type": "text", "style": {"code": true}}，
    不是 rich_text_inline_code 元素类型——v1.1 只认类型，真实产物漏保护。
    注意：rich_text_list 的 "style" 是字符串（"bullet"/"ordered"，Slack schema
    如此），只有 text/link 元素的 style 是 dict——先验类型再取键。
    """
    if node.get("type") in ("rich_text_preformatted", "rich_text_inline_code"):
        return True
    style = node.get("style")
    return isinstance(style, dict) and bool(style.get("code"))


def _process_nodes(nodes, parent_code=False):
    """递归重建：text 元素里的实体切成原生元素；代码节点整体跳过。

    代码节点（rich_text_preformatted / rich_text_inline_code / style.code 的
    text 元素）的子元素按 Slack 规范只许 text/link，绝不插入 user 元素——
    否则代码示例里的 <@U…> 变成真提及导致整块降级。
    """
    out, changed = [], False
    for node in nodes:
        if not isinstance(node, dict):
            out.append(node)
            continue
        is_code_node = _is_code_node(node)
        children = node.get("elements")
        if isinstance(children, list):
            new_children, ch = _process_nodes(children, parent_code or is_code_node)
            if ch:
                node = {**node, "elements": new_children}
                changed = True
            out.append(node)
            continue
        if (not is_code_node and not parent_code
                and (node.get("type") or "") == "text"
                and "<" in (node.get("text") or "")):
            parts = _split_text_element(node)
            if parts is not None:
                out.extend(parts)
                changed = True
                continue
        out.append(node)
    return out, changed


def _tokenize_blocks(blocks):
    """对渲染完成的 blocks 载荷做实体→原生元素后处理。"""
    if not isinstance(blocks, list):
        return blocks
    out, _ = _process_nodes(blocks)
    return out


# ---------------------------------------------------------------------------
# 成员表：users_list 分页全量，按 team_id 隔离，缓存 10 分钟
# {lower_name: (score, [uid...])}  score: 3=username 2=display/real
# ---------------------------------------------------------------------------

# 与树内 SlackAdapter._metadata_team_id 同源的 metadata keys（上游用
# _first_truthy 按序取第一个真值；source 子 dict 再查一遍同款 keys）。
_METADATA_TEAM_KEYS = ("scope_id", "slack_team_id", "team_id", "team",
                       "guild_id", "workspace_id")


def _first_truthy(d, keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _metadata_team_id(metadata):
    """从出站 metadata 取工作区 id（只读 dict，不碰 adapter 状态）。"""
    if not metadata:
        return ""
    found = _first_truthy(metadata, _METADATA_TEAM_KEYS)
    if found:
        return str(found)
    source = metadata.get("source") or {}
    if isinstance(source, dict):
        found = _first_truthy(source, _METADATA_TEAM_KEYS)
        if found:
            return str(found)
    return ""


def _team_key(adapter, chat_id=None, team_id=None):
    tid = team_id or ""
    if not tid and chat_id:
        tid = adapter._channel_team.get(chat_id) or ""
    return tid or ""


async def _name_table(adapter, chat_id=None, team_id=None):
    """(table,) 按 team 缓存；过期重建。

    **team 严格绑定**：显式 team_id 存在时**只用它**建表——绝不走
    _get_client 的 fallback 链（map 未知 → 单工作区兜底 → primary）。fallback
    只对「发消息」安全（发错工作区顶多报权限错）；对「名字→ID」是灾难（拿 A
    工作区的表解析 B 工作区的消息 = 指错人）。所以：

    - team_id 显式给定 → 固定该 team 的 client（_team_clients[team_id]），
      拿不到（未认证该工作区）→ 建空表（宁可不解析），绝不 fallback；
    - team_id 缺省 → 维持 chat 映射 → primary 的原路径（单工作区主场景）。
    """
    tables = getattr(adapter, _TABLES_ATTR, None)
    if tables is None:
        tables = {}
        setattr(adapter, _TABLES_ATTR, tables)
    key = _team_key(adapter, chat_id, team_id)
    now = time.monotonic()
    st = tables.get(key)
    if st and now - st[1] < _TABLE_TTL:
        return st[0]

    table = {}
    try:
        app = getattr(adapter, "_app", None)
        if app is not None:
            client = _team_client(adapter, team_id, chat_id)
            if client is not None:
                kwargs = {"limit": 200}
                while True:
                    resp = await client.users_list(**kwargs)
                    listed = resp.get("members", []) if isinstance(resp, dict) else []
                    for u in listed:
                        if u.get("deleted"):
                            continue
                        prof = u.get("profile", {})

                        def put(n, score):
                            k = str(n).strip().lower()
                            if not k:
                                return
                            cur = table.get(k)
                            if cur is None:
                                table[k] = (score, [u["id"]])
                            elif cur[0] < score:
                                table[k] = (score, [u["id"]])
                            elif cur[0] == score and u["id"] not in cur[1]:
                                table[k] = (score, cur[1] + [u["id"]])

                        if u.get("name"):
                            put(u["name"], 3)  # username：全 workspace 唯一 → 最高可靠
                        for n in (prof.get("display_name"), prof.get("real_name"),
                                  prof.get("display_name_normalized"),
                                  prof.get("real_name_normalized")):
                            if n:
                                put(n, 2)
                    cur = None
                    if isinstance(resp, dict):
                        meta = resp.get("response_metadata")
                        if isinstance(meta, dict):
                            cur = meta.get("next_cursor")
                    if cur:
                        kwargs["cursor"] = cur
                    else:
                        break
    except Exception:
        LOG.warning("[Slack] mention table build failed (team=%s)", key, exc_info=True)
        table = {}  # 失败也落缓存，避免每条消息重试打 API；TTL 到期自然重试
    tables[key] = (table, now)
    LOG.info("[Slack] mention table team=%s: %d entries", key or "<primary>", len(table))
    return table


def _team_client(adapter, team_id, chat_id):
    """建表用 client：显式 team_id → 固定 _team_clients[team_id]（无则 None）。

    team_id 缺省时才走 adapter._get_client(chat_id)（单工作区主场景，等价于
    primary）。**绝不**让显式 team_id 落进 _get_client 的 fallback 链。
    """
    if team_id:
        team_clients = getattr(adapter, "_team_clients", None) or {}
        return team_clients.get(team_id)  # 未认证该工作区 → None → 建空表
    try:
        return adapter._get_client(chat_id)
    except Exception:
        return None


def _sync_table_if_ready(adapter):
    """同步路径取表：仅当该 adapter 已有任一未过期缓存表（不 await、不建表）。

    _maybe_blocks 是同步签名（树内同步调用点），冷启动没表时宁可不解析，
    也不能阻塞/返回 coroutine。async 出口（send/_post_chunks/edit/seal）都会先
    await 建表，所以正常会话首条之前表就已就绪。
    """
    tables = getattr(adapter, _TABLES_ATTR, None)
    if not tables:
        return None, None
    now = time.monotonic()
    for key, (table, ts) in tables.items():
        if table and now - ts < _TABLE_TTL:
            return key, table
    return None, None


# ---------------------------------------------------------------------------
# 实例级包装
# ---------------------------------------------------------------------------


def _wrap_adapter(adapter):
    """实例级包装，幂等（connect/重连安全）。"""
    if getattr(adapter, _WRAP_ATTR, False):
        return
    setattr(adapter, _WRAP_ATTR, True)

    orig_maybe_blocks = adapter._maybe_blocks
    orig_post_chunks = adapter._post_chunks
    orig_edit_message = adapter.edit_message
    orig_seal_stream = adapter._seal_stream

    def _maybe_blocks_patched(content):
        # 必须保持同步签名（树内调用点同步取值）。解析只用现成缓存表。
        try:
            _, table = _sync_table_if_ready(adapter)
            if table:
                content = _build_repl_string(content, table)
        except Exception:
            pass
        blocks = orig_maybe_blocks(content)
        try:
            return _tokenize_blocks(blocks)
        except Exception:
            return blocks

    async def _resolve_async_outlet(content, chat_id=None, team_id=None, metadata=None):
        """async 出口统一解析：先确保表就绪（冷启动首条也解析），再替换。

        team 路由与树内出站一致：显式 team_id（_post_chunks 自带）>
        metadata 工作区（edit 的 _client_for 同源 keys）> chat 映射 > ""。
        """
        tid = team_id or _metadata_team_id(metadata) or _team_key(adapter, chat_id)
        try:
            table = await _name_table(adapter, chat_id=chat_id, team_id=tid)
            return _build_repl_string(content, table)
        except Exception:
            return content

    async def _post_chunks_patched(chat_id, team_id, content, formatted, thread_ts):
        formatted = await _resolve_async_outlet(formatted, chat_id, team_id)
        content = await _resolve_async_outlet(content, chat_id, team_id)
        return await orig_post_chunks(chat_id, team_id, content, formatted, thread_ts)

    async def _edit_message_patched(chat_id, message_id, content, *,
                                    finalize=False, metadata=None):
        content = await _resolve_async_outlet(content, chat_id=chat_id, metadata=metadata)
        return await orig_edit_message(chat_id, message_id, content,
                                       finalize=finalize, metadata=metadata)

    async def _seal_stream_patched(chat_id, stream, final_text=None, blocks=None):
        # 上游真实契约（树内 adapter._seal_stream）：
        #   def _seal_stream(self, chat_id, stream, final_text=None, blocks=None)
        #   final_text 是**完整终稿**；仅当 final_text.startswith(sent) 才发
        #   final_text[len(sent):] 增量（append-only，发了的收不回）。
        # 因此解析只施加于尚未流出的尾部：
        #   new_final = sent + resolve(final_text[len(sent):])
        # 解析整篇会破坏 startswith → 上游一个字增量都不发（丢内容）；已流入
        # stream 的 mention 无法追溯解析（append-only 的物理限制，接受）。
        # stream["sent"] 保持原样——上游靠它与 final_text 的差值算增量。
        if final_text is None:
            return await orig_seal_stream(chat_id, stream)
        sent = (stream or {}).get("sent", "")
        if not final_text.startswith(sent):
            return await orig_seal_stream(chat_id, stream, final_text=final_text,
                                          blocks=blocks)
        resolved_tail = await _resolve_async_outlet(
            final_text[len(sent):], chat_id=chat_id)
        new_final = sent + resolved_tail
        return await orig_seal_stream(chat_id, stream, final_text=new_final,
                                      blocks=blocks)

    adapter._maybe_blocks = _maybe_blocks_patched
    adapter._post_chunks = _post_chunks_patched
    adapter.edit_message = _edit_message_patched
    adapter._seal_stream = _seal_stream_patched
    LOG.info("[Slack] mention plugin v1.2: adapter wrapped (instance-level)")
    print("[slack-mention-plugin] adapter wrapped v1.2", flush=True)


def _factory(native, adapter):
    """ctx.register_platform_handler("slack", ...) 的 factory。"""
    try:
        _wrap_adapter(adapter)
    except Exception:
        LOG.error("[Slack] mention plugin wrap failed", exc_info=True)


def register(ctx):
    ctx.register_platform_handler("slack", _factory)
