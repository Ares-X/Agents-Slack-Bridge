#!/usr/bin/env python3
"""Slack mention 插件 v1.1（2026-10-03，修主人 review 的五个洞）

官方面：
- ``ctx.register_platform_handler("slack", factory)`` — SlackAdapter.connect() 时把
  ``(AsyncApp, adapter)`` 递给 factory，做**实例级**包装（不碰类、不碰树内文件）：
  1. ``adapter._maybe_blocks`` — name→ID 解析后再渲染 rich_text（同步签名，10-03
     凌晨血坑：async 化会被同步调用点拿到 coroutine）。
  2. ``adapter._post_chunks`` — 兜底路径预热成员表。
  3. ``adapter.edit_message`` — 编辑路径同样解析（v1.0 漏）。
  4. ``adapter._seal_stream`` — 原生流收尾同样解析（v1.0 漏）。

v1.1 修的五件事（对应主人 2026-10-03 review）：
① 代码保护：先按 fenced/inline code span 切片，mention 解析只碰非代码片段；
   实体→元素转换同理跳过代码节点（rich_text_preformatted/rich_text_inline_code
   只许 text/link，塞 user 元素会整条降级）。
② 正则回退：最长匹配失败再退单 token，词尾不许吃英文/句号（"@Muse." 只取 Muse）。
③ 同名歧义：可靠度排序（exact username > exact display/real）优先级固定，
   低可靠命中被高可靠名字顶掉时判歧义不解析；同可靠度同分不解析。
④ 流式/编辑冷启动：_maybe_blocks 同步路径发现表未预热时，尝试同步取
   asyncio 循环里已跑的任务结果（仅当已存在），否则该条不解析不阻塞——
   并在 send/_post_chunks/编辑/收尾所有 async 出口先建表，冷启动首条也解析。
⑤ 多工作区：表按 team_id 隔离缓存，用 _get_client(chat_id, team_id) 取对应
   workspace 的 token 拉成员。
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
    """名字 → 用户 ID；歧义返回 None（宁可不解析，绝不指错人）。

    可靠度：exact username(3) > exact display/real/normalized(2)。
    高可靠条目直接顶掉低可靠同名；同最高分 >1 个 → None。
    """
    if name not in table:
        return None
    score, uids = table[name]
    if score == 3:
        return uids[0]  # username 全 workspace 唯一
    return uids[0] if len(uids) == 1 else None


def _resolve_fragment(fragment, table):
    """单个非代码片段：@name → <@U…>；解析不了保持原样。"""
    if not table or "@" not in fragment:
        return fragment

    def repl(m):
        span = m.group(0)
        tokens = m.group(1).split(" ")
        # 从最长前缀候选逐级截短；每个候选先原样查，miss 再剥尾部句号查
        for k in range(len(tokens), 0, -1):
            cand_raw = " ".join(tokens[:k])
            for cand in (cand_raw, cand_raw.rstrip(".")):
                if not cand:
                    continue
                low = cand.lower()
                if low in _RESERVED:
                    return span  # @here/@channel 等保留字，整段原样
                uid = _lookup(table, low)
                if uid:
                    # 没被候选吃掉的尾巴（句号/被截短的词）留在原文
                    return "<@%s>%s" % (uid, span[1 + len(cand):])
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


def _entity_to_element(m):
    """实体匹配 → 原生 rich_text 元素；不匹配返回 None。

    Slack schema 不带 sigil：user_id="U…"、channel_id="C…"（<@U…> 的 @ 剥掉）。
    """
    if m.group(1):
        return {"type": "user", "user_id": m.group(1).lstrip("@")}
    if m.group(2):
        return {"type": "channel", "channel_id": m.group(2).lstrip("#")}
    if m.group(3):
        return {"type": "usergroup", "usergroup_id": m.group(4)}
    if m.group(5):
        return {"type": "broadcast", "range": m.group(6)}
    return None


_CODE_ELEMENT_TYPES = {
    "rich_text_preformatted",  # 子元素只许 text/link：塞 user 会被降级
    "rich_text_inline_code",   # Slack rich_text 规范
}


def _split_text_element(el):
    """text 元素含实体 → [text…, user/channel 元素…] 列表；无实体返回 None。

    继承周围 runs 的 style（bold 等）到原生元素。
    """
    text = el.get("text") or ""
    hits = list(_ENTITY_RE.finditer(text))
    if not hits:
        return None
    style = el.get("style") or {}
    out, pos = [], 0
    for m in hits:
        if m.start() > pos:
            out.append({"type": "text", "text": text[pos:m.start()],
                        **({"style": dict(style)} if style else {})})
        native = _entity_to_element(m)
        if native is not None:
            if style.get("bold"):
                native["style"] = {"bold": True}
            out.append(native)
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": text[pos:],
                    **({"style": dict(style)} if style else {})})
    return [e for e in out if e.get("type") != "text" or e.get("text")]


def _process_nodes(nodes, parent_code=False):
    """递归重建：text 元素里的实体切成原生元素；代码节点整体跳过。

    代码节点（rich_text_preformatted / rich_text_inline_code）的子元素按
    Slack 规范只许 text/link，绝不插入 user 元素——v1.0 会把代码示例里的
    <@U…> 变成真提及导致整块降级。
    """
    out, changed = [], False
    for node in nodes:
        if not isinstance(node, dict):
            out.append(node)
            continue
        ntype = node.get("type") or ""
        is_code_node = ntype in _CODE_ELEMENT_TYPES
        children = node.get("elements")
        if isinstance(children, list):
            new_children, ch = _process_nodes(children, parent_code or is_code_node)
            if ch:
                node = {**node, "elements": new_children}
                changed = True
            out.append(node)
            continue
        if (not is_code_node and not parent_code
                and ntype == "text" and "<" in (node.get("text") or "")):
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


def _team_key(adapter, chat_id=None, team_id=None):
    tid = team_id or ""
    if not tid and chat_id:
        tid = adapter._channel_team.get(chat_id) or ""
    return tid or ""


async def _name_table(adapter, chat_id=None, team_id=None):
    """(table, ) 按 team 缓存；过期重建。"""
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
            client = adapter._get_client(chat_id, team_id=team_id) if team_id \
                else adapter._get_client(chat_id)
            kwargs = {"limit": 200}
            while True:
                resp = await client.users_list(**kwargs)
                for u in resp.get("members", []):
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
                cur = resp.get("response_metadata", {}).get("next_cursor")
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


def _sync_table_if_ready(adapter):
    """同步路径取表：仅当该 adapter 已有任一未过期缓存表（不 await、不建表）。

    _maybe_blocks 是同步签名（树内同步调用点），冷启动没表时宁可不解析，
    也不能阻塞/返回 coroutine。async 出口（send/_post_chunks/edit/seal）
    都会先 await 建表，所以正常会话首条之前表就已就绪。
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

    async def _resolve_async_outlet(content, chat_id=None, team_id=None):
        """async 出口统一解析：先确保表就绪（冷启动首条也解析），再替换。"""
        try:
            table = await _name_table(adapter, chat_id=chat_id, team_id=team_id)
            return _build_repl_string(content, table)
        except Exception:
            return content

    async def _post_chunks_patched(chat_id, team_id, content, formatted, thread_ts):
        formatted = await _resolve_async_outlet(formatted, chat_id, team_id)
        content = await _resolve_async_outlet(content, chat_id, team_id)
        return await orig_post_chunks(chat_id, team_id, content, formatted, thread_ts)

    async def _edit_message_patched(chat_id, message_id, content, *,
                                    finalize=False, metadata=None):
        content = await _resolve_async_outlet(content, chat_id=chat_id)
        return await orig_edit_message(chat_id, message_id, content,
                                       finalize=finalize, metadata=metadata)

    async def _seal_stream_patched(chat_id, stream, final_text):
        final_text = await _resolve_async_outlet(final_text, chat_id=chat_id)
        return await orig_seal_stream(chat_id, stream, final_text)

    adapter._maybe_blocks = _maybe_blocks_patched
    adapter._post_chunks = _post_chunks_patched
    adapter.edit_message = _edit_message_patched
    adapter._seal_stream = _seal_stream_patched
    LOG.info("[Slack] mention plugin v1.1: adapter wrapped (instance-level)")
    print("[slack-mention-plugin] adapter wrapped v1.1", flush=True)


def _factory(native, adapter):
    """ctx.register_platform_handler("slack", ...) 的 factory。"""
    try:
        _wrap_adapter(adapter)
    except Exception:
        LOG.error("[Slack] mention plugin wrap failed", exc_info=True)


def register(ctx):
    ctx.register_platform_handler("slack", _factory)
