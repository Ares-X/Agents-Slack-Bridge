#!/usr/bin/env python3
"""Slack mention 插件 v1.3（2026-10-03，PR #10 二轮复核三项）

官方面（不变）：
- ``ctx.register_platform_handler("slack", factory)`` — SlackAdapter.connect() 时把
  ``(AsyncApp, adapter)`` 递给 factory，做**实例级**包装（不碰类、不碰树内文件）：
  1. ``adapter._maybe_blocks`` — name→ID 解析后再渲染 rich_text（同步签名，10-03
     凌晨血坑：async 化会被同步调用点拿到 coroutine）。
  2. ``adapter._post_chunks`` — 兜底路径预热成员表。
  3. ``adapter.edit_message`` — 编辑路径同样解析（v1.0 漏）。
  4. ``adapter._commit_stream`` — 原生流收尾解析（v1.3 换位：v1.2 包的
     ``_seal_stream`` 是旧上游契约，新上游文本收尾全部走 ``_commit_stream``）。

v1.3 修的三件事（对齐上游 a5e7df27c7 / b3059921bc 的真实契约）：
① ``_commit_stream`` 才是带文本的收尾路径，真实签名
   ``(key, stream, text, metadata, delta="", replace=False)``：
   - ``key`` 是 ``(team_id, chat_id, thread_ts)`` 三元组（key[0] 即权威 team，
     建表路由直接用它）；
   - ``delta`` 是**未流出的尾段**——``chat.stopStream.markdown_text`` 是 APPEND
     语义，上游不变式 ``text.endswith(delta)``（由 ``_stream_relation`` 切出）；
   - ``replace=True`` 表示终稿被改写、不与 sent 前缀对齐，由 chat.update 整篇替换。
   解析施加面：replace → 整篇解析（纯 update 载荷，无 append 约束）；delta 非空 →
   只解析尾段并同步改写 ``text`` 尾部（保持 ``text.endswith(delta)`` 与 sent 前缀
   关系）；delta 空 → 原样透传。上游对 ``_seal_stream`` 的其余直呼（片段切换封口、
   前缀断裂封口、超龄清理、断连清理）都**不带文本**，无需包装——包了反而要猜旧签名。
   已流入 stream 的 mention 无法追溯解析（append-only 物理限制，接受）；富文本路径
   的 chat.update 会带解析后的整篇 blocks，等于收尾时把可见正文重新排一次版。
② ``users_list`` 响应是 ``AsyncSlackResponse``：支持 ``.get()`` 但**不是 dict**。
   ``isinstance(resp, dict)`` 判断会把整页成员丢掉、分页提前终止、缓存空表。
   改为 ``hasattr(resp, "get")`` 鸭子型取值（dict 与 SlackResponse 通吃）。
③ 同步取表严格绑工作区：有目标上下文（显式 team_id / chat 映射 / metadata）→
   只认该 team 的表，未就绪宁可不解析；无上下文（``_maybe_blocks(content)`` 只有
   正文）→ 仅当缓存里**只有一个** team 键时用它（单工作区主场景），多 team 键 =
   目标工作区无法确定 → 保留原文。绝不禁用「任取第一个表」——那是拿 A 工作区的
   名字解析 B 工作区的消息。

v1.2 保留的修复（未回退）：真实 renderer 的 ``style.code`` 代码保护、「查无」与
「歧义」分离（歧义立即停，绝不回退吃更短的名字）、成员表严格绑定目标 team、
edit 路径 metadata 工作区路由。

v1.1 保留的既有修复：短名回退（"@Muse please review"→只吃 Muse）、句末标点
（"@Muse."→<@U1>.）、围栏/内联代码片段的文本级保护。
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
# lookbehind 排除 '<'：已解析的实体 "<@U1>" 内部不再当候选（若某用户名恰为
# "u1"，二次解析会把 "<@U1>" 改写成 "<<@UID>1>"——幂等性防线）。
_TOKEN = r"[A-Za-z0-9_.\-]+"
_MENTION_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9_.@>\-<])@(" + _TOKEN + r"(?:[ ]+" + _TOKEN + r")*)"
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

_METADATA_TEAM_KEYS = ("scope_id", "slack_team_id", "team_id", "team",
                       "guild_id", "workspace_id")
# source 子 dict 层上游只认这四个（窄于顶层），见树内 _metadata_team_id。
_SOURCE_TEAM_KEYS = ("scope_id", "slack_team_id", "team_id", "guild_id")


def _first_truthy(d, keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _metadata_team_id(metadata):
    """从出站 metadata 取工作区 id（只读，不碰 adapter 状态）。

    与树内 SlackAdapter._metadata_team_id 同构：顶层 keys 全集 → source 子 dict
    用窄 keys → source 是对象时 getattr(scope_id/guild_id)。任何形状都不抛。
    """
    if not metadata:
        return ""
    if not isinstance(metadata, dict):
        value = getattr(metadata, "scope_id", None) or getattr(metadata, "team_id", None)
        return str(value) if value else ""
    found = _first_truthy(metadata, _METADATA_TEAM_KEYS)
    if found:
        return str(found)
    source = metadata.get("source")
    if isinstance(source, dict):
        found = _first_truthy(source, _SOURCE_TEAM_KEYS)
        if found:
            return str(found)
    elif source is not None:
        value = getattr(source, "scope_id", None) or getattr(source, "guild_id", None)
        if value:
            return str(value)
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
                    # 响应是 AsyncSlackResponse：支持 .get() 但**不是 dict**
                    # （isinstance(resp, dict) 会整页丢弃+分页终止+缓存空表）。
                    # 鸭子型取值，dict 与 SlackResponse 通吃。
                    listed = resp.get("members") or [] if hasattr(resp, "get") else []
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
                    meta = resp.get("response_metadata") if hasattr(resp, "get") else None
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


def _sync_table_if_ready(adapter, chat_id=None, team_id=None, metadata=None):
    """同步路径取表：仅用**目标工作区**的现成缓存表（不 await、不建表）。

    工作区上下文解析（与 async 出口同一条链）：
      显式 team_id > metadata 工作区 keys > chat 映射（_channel_team）。
    - 有上下文 → 只认该 team 的表；未就绪/未过期不存在 → 宁可不解析。
    - 无任何上下文（``_maybe_blocks(content)`` 只有正文）→ 仅当缓存里恰好
      **只有一个** team 键时用它（单工作区主场景的既有多数行为）；
      多个 team 键 = 目标工作区无法确定 → 保留原文。
    绝不「任取第一个表」——那是拿 A 工作区的名字表解析 B 工作区的消息 = 指错人。
    _maybe_blocks 是同步签名（树内同步调用点），这里不能阻塞/返回 coroutine；
    async 出口（send/_post_chunks/edit/commit）都会先 await 建表，正常会话首条
    之前表就已就绪。
    """
    tables = getattr(adapter, _TABLES_ATTR, None)
    if not tables:
        return None, None
    now = time.monotonic()
    tid = team_id or _metadata_team_id(metadata)
    if not tid and chat_id:
        tid = (getattr(adapter, "_channel_team", None) or {}).get(chat_id) or ""
    if tid:
        st = tables.get(tid)
        if st and st[0] and now - st[1] < _TABLE_TTL:
            return tid, st[0]
        return None, None
    fresh = [(k, st[0]) for k, st in tables.items() if st[0] and now - st[1] < _TABLE_TTL]
    if len(fresh) == 1:
        return fresh[0]
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
    orig_commit_stream = adapter._commit_stream

    # 目标工作区 id 的单一解析链：显式 team_id > metadata keys > chat 映射。
    def _target_team_id(chat_id=None, team_id=None, metadata=None):
        return team_id or _metadata_team_id(metadata) or _team_key(adapter, chat_id)

    def _maybe_blocks_patched(content):
        # 必须保持同步签名（树内调用点同步取值）。解析只用现成缓存表。
        # 树内 _maybe_blocks(content) 只有正文、无工作区上下文——按
        # _sync_table_if_ready 的「唯一 team 键或保留原文」规则取表。
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
        tid = _target_team_id(chat_id, team_id, metadata)
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

    async def _commit_stream_patched(key, stream, text, metadata=None, *,
                                     delta="", replace=False):
        # 上游真实契约（a5e7df27c7 / b3059921bc，plugins/platforms/slack/adapter.py）：
        #   async def _commit_stream(self, key, stream, text, metadata=None, *,
        #                             delta="", replace=False)
        # - key = (team_id, chat_id, thread_ts)——key[0] 是权威 team（树内
        #   _stream_key 用 metadata/_channel_team 建的），建表直接用它路由；
        # - delta 是未流出尾段：orig 内部把 delta 原样递给 _seal_stream 的
        #   chat.stopStream(markdown_text=delta)——APPEND 语义。所以解析后的
        #   尾段必须**同时**改写 text 尾部与 delta 本身，两者保持一致；
        # - replace=True：终稿被改写（不与 sent 前缀对齐），走 chat.update 整篇
        #   替换——纯 update 载荷，无 append 约束，可整篇解析；
        # - delta=""：纯封口（片段切换/清理），无文本可解析，原样透传；
        # - extends 不变量被打破的防御形状：原样透传，宁可不解析（整篇解析会
        #   让 stopStream 追加一段与 sent 不接续的文本 = 重复内容）。
        team_id = key[0] if isinstance(key, (tuple, list)) and key else None
        if not text or (not delta and not replace):
            return await orig_commit_stream(
                key, stream, text, metadata, delta=delta, replace=replace)
        if replace:
            new_text = await _resolve_async_outlet(text, team_id=team_id)
            return await orig_commit_stream(
                key, stream, new_text, metadata, delta=delta, replace=replace)
        sent = (stream or {}).get("sent", "")
        if not text.endswith(delta) or not text.startswith(sent):
            return await orig_commit_stream(
                key, stream, text, metadata, delta=delta, replace=replace)
        head = text[:len(text) - len(delta)]  # == sent（extends 不变量），原样
        resolved_tail = await _resolve_async_outlet(delta, team_id=team_id)
        new_text = head + resolved_tail
        return await orig_commit_stream(
            key, stream, new_text, metadata, delta=resolved_tail, replace=replace)

    adapter._maybe_blocks = _maybe_blocks_patched
    adapter._post_chunks = _post_chunks_patched
    adapter.edit_message = _edit_message_patched
    adapter._commit_stream = _commit_stream_patched
    LOG.info("[Slack] mention plugin v1.3: adapter wrapped (instance-level)")
    print("[slack-mention-plugin] adapter wrapped v1.3", flush=True)


def _factory(native, adapter):
    """ctx.register_platform_handler("slack", ...) 的 factory。"""
    try:
        _wrap_adapter(adapter)
    except Exception:
        LOG.error("[Slack] mention plugin wrap failed", exc_info=True)


def register(ctx):
    ctx.register_platform_handler("slack", _factory)
