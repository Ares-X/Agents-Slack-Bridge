#!/usr/bin/env python3
"""Slack mention 插件（2026-10-03，替树内补丁的零 core 版）

官方面：
- ``ctx.register_platform_handler("slack", factory)`` — gateway/platforms/base.py
  ``_wire_plugin_handlers`` 在 SlackAdapter.connect() 时把 ``(AsyncApp, adapter)`` 递给
  factory。在 factory 里做**实例级**包装（挂在 adapter 实例上，不碰类、不碰树内文件）：
  1. ``adapter._maybe_blocks`` — name→ID 解析后再渲染 rich_text。
  2. ``adapter._post_chunks`` — 兜底路径：多块/降级路径的 formatted 文本同样解析。
成员表：users_list 分页全量，缓存 10 分钟，per-adapter 实例属性。
"""

import logging
import re
import time

LOG = logging.getLogger("slack.mention_fix")

# 前面不能是词字符/邮箱中缀；名字=单词(+空格+单词)*；后面是空白/标点/行尾
_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_.@>\-])@([A-Za-z0-9_.\-]+(?:[ ]+[A-Za-z0-9_.\-]+)*)(?=[\s,.;:!?)\]}\"'：，。；！？）]|$)"
)
_RESERVED = {"here", "channel", "everyone", "all"}
_TABLE_TTL = 600.0

_WRAP_ATTR = "_mention_wrap_done"
_TABLE_ATTR = "_mention_table_state"

# <@U…> / <#C…> / <!subteam^S…> / <!here|<!channel |<!everyone>
_ENTITY_RE = re.compile(
    r"<(?:(@[A-Z0-9]+)|(#[A-Z0-9]+)|(!subteam\^([A-Z0-9]+))|(!(here|channel|everyone)))>"
)


def _entity_to_element(m):
    """实体匹配 → 原生 rich_text 元素；不匹配返回 None。

    Slack schema 不带 sigil：user_id="U…"、channel_id="C…"（<@U…> 的 @ 要剥掉）。
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


def _split_text_element(el):
    """text 元素含实体 → [text…, user/channel 元素…] 列表；无实体返回 None。

    继承周围 runs 的 style（bold 等）到原生元素，与 rich_text 规范一致。
    """
    text = el.get("text") or ""
    hits = list(_ENTITY_RE.finditer(text))
    if not hits:
        return None
    style = el.get("style") or {}
    out, pos = [], 0
    for m in hits:
        if m.start() > pos:
            out.append({"type": "text", "text": text[pos:m.start()], **({"style": dict(style)} if style else {})})
        native = _entity_to_element(m)
        if native is not None:
            if style.get("bold"):
                native["style"] = {"bold": True}
            out.append(native)
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": text[pos:], **({"style": dict(style)} if style else {})})
    return [e for e in out if e.get("type") != "text" or e.get("text")]


def _process_nodes(nodes):
    """递归重建元素列表：text 元素里的实体切成原生元素。

    返回 (new_nodes, changed)。结构：block.elements → rich_text_section/
    rich_text_quote/rich_text_list.elements → text 元素。section/mrkdwn 块没有
    elements 键，天然跳过（mrkdwn 文本里 <@U…> 由 Slack 原生解析）。
    """
    out, changed = [], False
    for node in nodes:
        if not isinstance(node, dict):
            out.append(node)
            continue
        children = node.get("elements")
        if isinstance(children, list):
            new_children, ch = _process_nodes(children)
            if ch:
                node = {**node, "elements": new_children}
                changed = True
            out.append(node)
            continue
        if node.get("type") == "text" and "<" in (node.get("text") or ""):
            parts = _split_text_element(node)
            if parts is not None:
                out.extend(parts)
                changed = True
                continue
        out.append(node)
    return out, changed


def _tokenize_blocks(blocks):
    """对渲染完成的 blocks 载荷做实体→原生元素后处理（sanitize 之后跑也安全）。"""
    if not isinstance(blocks, list):
        return blocks
    out, _ = _process_nodes(blocks)
    return out


def _resolve(text, table):
    """把 "@Display Name"/"@user_name" 换成 <@U...>；保留词/陌生名字原样。"""
    if not table or "@" not in text:
        return text

    def repl(m):
        raw = m.group(1).strip().lower()
        if raw in _RESERVED:
            return m.group(0)
        uid = table.get(raw)
        return "<@%s>" % uid if uid else m.group(0)

    return _MENTION_RE.sub(repl, text)


async def _name_table(adapter):
    """users_list 分页全量 → {小写名字: 用户ID}，缓存 10 分钟。"""
    st = getattr(adapter, _TABLE_ATTR, None)
    now = time.monotonic()
    if st and now - st[1] < _TABLE_TTL:
        return st[0]
    table = {}
    try:
        app = getattr(adapter, "_app", None)
        if app is not None:
            client = app.client
            kwargs = {"limit": 200}
            while True:
                resp = await client.users_list(**kwargs)
                for u in resp.get("members", []):
                    if u.get("deleted"):
                        continue
                    prof = u.get("profile", {})
                    for n in (prof.get("display_name"), prof.get("real_name"),
                              u.get("name"),
                              prof.get("display_name_normalized"),
                              prof.get("real_name_normalized")):
                        if n:
                            table[str(n).strip().lower()] = u["id"]
                cur = resp.get("response_metadata", {}).get("next_cursor")
                if cur:
                    kwargs["cursor"] = cur
                else:
                    break
    except Exception:
        LOG.warning("[Slack] mention table build failed", exc_info=True)
    setattr(adapter, _TABLE_ATTR, (table, now))
    LOG.info("[Slack] mention table: %d entries", len(table))
    return table


def _wrap_adapter(adapter):
    """实例级包装，幂等（connect/重连安全）。"""
    if getattr(adapter, _WRAP_ATTR, False):
        return
    setattr(adapter, _WRAP_ATTR, True)

    orig_maybe_blocks = adapter._maybe_blocks
    orig_post_chunks = adapter._post_chunks

    def _maybe_blocks_patched(content):
        # 树内 _maybe_blocks 是同步方法（adapter.py:2986 def 非 async def），
        # 调用点 `self._maybe_blocks(content)` 同步取值 → 这里必须保持同步签名。
        # 名字解析只用缓存表（表由 _post_chunks_patched 的 async 路径预热）；
        # 表未预热时跳过解析，绝不 await、绝不返回 coroutine。
        try:
            st = getattr(adapter, _TABLE_ATTR, None)
            if st:
                content = _resolve(content, st[0])
        except Exception:
            pass
        blocks = orig_maybe_blocks(content)
        try:
            return _tokenize_blocks(blocks)
        except Exception:
            return blocks

    async def _post_chunks_patched(chat_id, team_id, content, formatted, thread_ts):
        try:
            formatted = _resolve(formatted, await _name_table(adapter))
        except Exception:
            pass
        return await orig_post_chunks(chat_id, team_id, content, formatted, thread_ts)

    adapter._maybe_blocks = _maybe_blocks_patched
    adapter._post_chunks = _post_chunks_patched
    LOG.info("[Slack] mention plugin: adapter wrapped (instance-level)")
    print("[slack-mention-plugin] adapter wrapped", flush=True)


def _factory(native, adapter):
    """ctx.register_platform_handler("slack", ...) 的 factory。"""
    try:
        _wrap_adapter(adapter)
    except Exception:
        LOG.error("[Slack] mention plugin wrap failed", exc_info=True)


def register(ctx):
    ctx.register_platform_handler("slack", _factory)
