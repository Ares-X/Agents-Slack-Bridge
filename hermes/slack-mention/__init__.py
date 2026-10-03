#!/usr/bin/env python3
"""Slack mention 插件 v1.4（2026-10-03，部署树 836b5f8 契约对齐）

官方面（不变）：
- ``ctx.register_platform_handler("slack", factory)`` — SlackAdapter.connect() 时把
  ``(AsyncApp, adapter)`` 递给 factory，做**实例级**包装（不碰类、不碰树内文件）：
  1. ``adapter._maybe_blocks`` — name→ID 解析后再渲染 rich_text（同步签名，10-03
     凌晨血坑：async 化会被同步调用点拿到 coroutine）。
  2. ``adapter._post_chunks`` — 兜底路径预热成员表。
  3. ``adapter.edit_message`` — 编辑路径同样解析（v1.0 漏）。
  4. ``adapter._commit_stream`` — 新上游（b3059921bc 起）原生流收尾解析。
  5. ``adapter._try_finalize_stream`` — **旧上游（836b5f8 部署树）**的等价收尾
     出口（v1.4 新增）：老树无 _commit_stream，文本收尾全走
     ``send() → _try_finalize_stream(chat_id, content) → _seal_stream``，
     且在 send() 顶部**先于** _post_chunks 执行——v1.3 的 async 预热出口在该
     路径永不触发，收尾 stopStream 的未流出尾段与 finalize chat.update 的
     blocks 全是生文本（冷缓存下 mention 不解析）。

v1.4（836 兼容层）：
- 仅当 ``_commit_stream`` 取不到（老树档）才包 ``_try_finalize_stream``——
  新树上 send() 经 _try_finalize → _commit_stream（已包），绝不双包。
- 老树流身份 = ``_active_streams`` 的 **chat_id 索引**（非三元组 key）；extends
  判定用老树自己的字节前缀语义（sent 非空且 text.startswith(sent)），delta =
  text[len(sent):]，floor = len(sent)——已发送前缀字节不动，只解析尾段提及
  （与 v1.3 round 3 #2 同一条铁律）。
- 解析后的整篇 final_text 递给 orig：orig 内部自切 stopStream 的 append delta
  （final_text[len(sent):] = 已解析尾段），finalize blocks 由 orig 的
  ``self._maybe_blocks`` 渲染——那是**包装后**的同步出口，靠 contextvar 直通
  窗口跳过它的全文再解析（前缀 @name 已上屏，二次解析 = text/blocks 不一致）。
- orig 签名两档自适应：老树 ``(chat_id, content)``，带 metadata 的变体照传。
- 建表路由与 async 出口同链：metadata team > chat 映射 > 唯一认证工作区；
  解析不出目标且多工作区时保留原文（宁可不解析，绝不拿 A 表猜 B）。

v1.3 r5 保留：直通 edit 原样送达已构造载荷（纯文本部署下「没有 blocks」≠
「text 缺解析」，final_blocks 探测已撤销）。
v1.3 r4 保留：contextvar 流身份 (team, channel, ts)、primary-team 证明、
跨工作区并发收尾互不干扰。
v1.3 r3 保留：_commit_stream 真实契约（key/delta/replace）、尾段解析的
floor 限定与完整终稿判界、_stream_relation 两档 extends 委托。
v1.2/v1.1 保留：代码切片保护、「查无/歧义」分离、成员表严格绑定目标 team。
"""

import asyncio
import contextvars
import logging
import re
import time

LOG = logging.getLogger("slack.mention_fix")

_RESERVED = {"here", "channel", "everyone", "all"}
_TABLE_TTL = 600.0

_WRAP_ATTR = "_mention_wrap_done"
_TABLES_ATTR = "_mention_team_tables"  # {team_id|"": (table, mono_ts)}

# v1.3 r4（评审第三轮 #2）：流收尾期间的原位 finalize edit 直通标记，不再用
# 适配器上的全局 set(ts)/计数，而是 contextvar —— 直通/抑制窗口只对当前
# 调用上下文（task + 其 await 子树）可见：
# - 不同工作区/频道的同 ts 流并发收尾互不干扰（A 完成不再撤销 B 的保护，
#   B 的无关普通编辑不再被 A 的窗口错误跳过提及解析）；
# - 嵌套同步出口（_commit_stream 内 `not self._maybe_blocks(text)` 的探测
#   调用）与外层 async 同 task，天然继承上下文，也无需再单独抑制；
# - edit_message 的直通判定按调用现场解析目标 (team, channel, ts) 精确命中。
# contextvar 在同步包装（非 async def）里也可 set（无运行循环要求），而其
# 嵌套 async 函数（如 _commit_stream 内部调用）会继承当前值。
_STREAM_CTX_NAME = "asb_slack_mention_stream_edit"
_stream_edit_ctx = contextvars.ContextVar(_STREAM_CTX_NAME, default=None)

# v1.3 r4（评审 #1）：primary fallback 工作区（上游 `_get_client` 最终档
# `return self._app.client` 实际路由到的工作区）的缓存属性名。生产里
# `_app.client` 与 `_team_clients[primary]` 是**不同对象**（同 token 各建一
# 个，connect() 1821 行），对象身份在生产必失败——所以三层解析见
# `_primary_team_id`：对象身份 → client.team_id 属性 → token 对账。
_PRIMARY_TEAM_ATTR = "_mention_primary_team"

# <@U…> / <#C…> / <!subteam^S…> / <!here|<!channel|<!everyone>
_ENTITY_RE = re.compile(
    r"<(?:(@[A-Z0-9]+)|(#[A-Z0-9]+)|(!subteam\^([A-Z0-9]+))|(!(here|channel|everyone)))>"
)

# ---------------------------------------------------------------------------
# 代码切片：mention 解析绝不碰 fenced code / inline code span 的内容
# 原样保留分隔串，解析后原位拼回（保序，不重排）。
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```.*?```|```.*|`[^`\n]*`", re.S)


def _split_code(text):
    """→ [(fragment, is_code)]，片段拼接还原原文。

    未闭合的 ``` 围栏视为代码到 EOF（与 Slack 渲染一致）——跨流式边界时
    draft 开了栏、final 还没合栏是常态，此时围栏内的 @ 不是提及（round 3 #2）。
    """
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


def _primary_team_id(adapter):
    """primary fallback 实际路由到的工作区 id（上游 `_get_client` 最终档），

    生产真实形状（connect()，1821 行起）：``AsyncApp(token=t0, client=web0)`` 与
    ``_team_clients[T0] = web1`` 是**同 token 的不同对象**——auth.test 只给
    ``_team_clients`` 添 team 名，``_app.client`` 上没有。所以按序三层：
      ① 对象身份：``_app.client is _team_clients[tid]``（测试桩/同对象捷径）；
      ② 属性：``client.team_id``（部分桩显式携带）；
      ③ token 对账：``client.token == _team_clients[tid].token``（生产路径，
         首个 bot token 既是 primary 又必在 map 里，auth.test 保证）。
    三层全空（未认证/单测空 map）返回 ""；结果缓存到 adapter 属性（键带
    ``id(_app)``，重连换 ``_app`` 对象时缓存即失效），避免每条无上下文消息
    都跑 getattr 链。
    """
    app = getattr(adapter, "_app", None)
    client = getattr(app, "client", None)
    if client is None:
        return ""
    cached = getattr(adapter, _PRIMARY_TEAM_ATTR, None)
    if cached is not None and cached[0] == id(app):
        return cached[1]
    team_id = ""
    for tid, mapped in (getattr(adapter, "_team_clients", None) or {}).items():
        if mapped is client:
            team_id = str(tid)
            break
    if not team_id:
        attr = getattr(client, "team_id", None)
        if attr:
            team_id = str(attr)
    if not team_id:
        primary_token = getattr(client, "token", None)
        if primary_token:
            for tid, mapped in (getattr(adapter, "_team_clients", None) or {}).items():
                if getattr(mapped, "token", None) == primary_token:
                    team_id = str(tid)
                    break
    setattr(adapter, _PRIMARY_TEAM_ATTR, (id(app), team_id))
    return team_id


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
    - 有上下文 → 只认该 team 的表；未就绪/过期不存在 → 宁可不解析。
    - 无任何上下文（``_maybe_blocks(content)`` 只有正文）→ 仅当缓存里
      **只见过一个工作区**且它就是上游 primary fallback 的工作区时用它；
      否则目标工作区无法确定 → 保留原文（r4 #1：唯一缓存不能证明目标）。
    「见过」按缓存键全集计（含空表/失败表/过期表）——空表和失败表同样是
    「第二个工作区存在」的证据；若只数新鲜非空表，B 建表失败会把 A 变成
    「唯一幸存者」，等于拿 A 的表猜 B（round 3 #1：预热 A、B 的
    users.list 空或失败，随后向 B 发/编富文本，blocks 引用 A 的用户 ID）。
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
    # 无上下文（评审 r4 #1）：「唯一缓存表」不能证明目标工作区——必须要求
    # 「缓存里只见过一个 team 键」**且**该键就是上游 primary fallback 实际
    # 路由到的工作区（_app.client.team_id）。A/B 双工作区只预热 B 时，上游
    # _get_client 把无上下文消息发去 A 的客户端：此时唯一表 B 与目标 A 不
    # 一致 → text 与 blocks 都保留原文；同一条消息两处指人不一致的根源
    # 就在此。只有「唯一键 == primary team」才解析（单工作区主场景，及
    # 双工作区但 primary 已预热且确无他表时）。
    if len(tables) == 1:
        k, st = next(iter(tables.items()))
        if st[0] and now - st[1] < _TABLE_TTL and k == _primary_team_id(adapter):
            return k, st[0]
    return None, None


def _stream_extends(adapter, stream, text, delta):
    """终稿 ``text`` 与已发送 ``sent`` 的 extends 关系判定（round 3 #3）。

    优先用上游真身 ``_stream_relation``——delta 本来就是它切出来的，权威且
    免猜：它认两档 extends（字节前缀；或去除前导空白后对齐 ``sent.strip()``）。
    包装此前自己要求 ``text.startswith(sent)``，把上游合法的第二档（终稿剥了
    sent 的尾部空白，如 sent="Done @Muse  " / final="Done @Muse and @Muse"）
    误判为断裂，导致尾段提及漏解析。老树取不到该方法时退回字节前缀判定
    （老树的 delta 就是按字节前缀切的，退回即精确），不会比 v1.3 更严。
    """
    sent = (stream or {}).get("sent", "")
    relation = getattr(adapter, "_stream_relation", None)
    if callable(relation):
        try:
            rel = relation(sent, text)
        except Exception:
            rel = None
        if isinstance(rel, tuple) and len(rel) == 2:
            kind, rel_delta = rel
            if kind == "extends":
                return rel_delta == delta
            if kind == "equal":
                return not delta.strip()
        return False
    if not sent:
        return False
    if not text.startswith(sent):
        return False
    return text[len(sent):] == delta


# ---------------------------------------------------------------------------
# 实例级包装
# ---------------------------------------------------------------------------


def _resolve_tail_mentions(text, table, floor):
    """只改写 ``floor`` 之后**开始**的提及，其余字节原样（round 3 #2）。

    为什么不能只解析 delta：提及的合法性依赖前文字节——``support`` 已发送、
    尾段是 ``@Muse`` 时是词内 @，不是提及；draft 以反引号收尾、终稿在代码里
    补出 ``@Muse`` 时是代码。所以代码围栏与词边界都在**完整终稿**上判定
    （_MENTION_SPAN_RE 的 lookbehind 直接吃全文上下文）；但改写面限定在
    未发送尾段——已发送前缀的字节已经出现在 Slack 上，动了就重复/错写。

    规则：span 起点 >= floor 且不在代码区才可改写；起点 < floor 的（含被
    边界切开的半个提及，如已发送 ``@Mu`` + 尾段 ``se``）原样保留。
    """
    if not table or "@" not in text or floor >= len(text):
        return text
    code_ranges = [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]

    def in_code(i):
        return any(a <= i < b for a, b in code_ranges)

    out, pos = [], 0
    for m in _MENTION_SPAN_RE.finditer(text):
        if m.start() < floor or in_code(m.start()):
            continue
        out.append(text[pos:m.start()])
        out.append(_resolve_fragment(m.group(0), table))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _stream_target_key(adapter, chat_id, message_id, metadata=None):
    """edit_message 包装侧：按调用现场把 (team, channel, ts) 解析成流身份键。

    team 解析顺序刻意与上游出站路由的**实际效果**一致（见 `_get_client`）：
    metadata 显式 team > chat 映射（`_channel_team`，入站事件学习）>
    `scope_id_for_chat`（含「唯一认证工作区」档）> 主客户端的 team。
    同一条链同时供 `_pass_through_edit`（登记侧，key[0] 即 metadata team）
    与 `_edit_message_patched`（判定侧）使用——两侧解析函数一致，时序差异
    不影响命中；同 ts 不同 (team, channel) 的流天然互不干扰（评审 #2）。
    """
    team_id = _metadata_team_id(metadata) or _team_key(adapter, chat_id)
    if not team_id:
        team_id = _primary_team_id(adapter)
    return (str(team_id or ""), str(chat_id or ""), str(message_id or ""))


def _stream_identity(adapter, key, ts):
    """_pass_through_edit 登记侧的流身份 = (team, channel, ts)。

    team 取 key[0] 优先——上游 `_stream_key` 建 key 时就是
    ``metadata team or scope_id_for_chat``，收尾的 finalize edit 又透传同一
    metadata，两侧天然对齐；key[0] 空（无 metadata 的流）时退 chat 映射 →
    primary，仍与 edit 侧（metadata 空）同链。
    """
    if ts is None:
        return None
    chat_id = key[1] if isinstance(key, (tuple, list)) and len(key) > 1 else None
    team_id = (key[0] if isinstance(key, (tuple, list)) and key else None) \
        or _team_key(adapter, chat_id) or _primary_team_id(adapter)
    return (str(team_id or ""), str(chat_id or ""), str(ts))


def _wrap_adapter(adapter):
    """实例级包装，幂等（connect/重连安全）。"""
    if getattr(adapter, _WRAP_ATTR, False):
        return
    setattr(adapter, _WRAP_ATTR, True)

    orig_maybe_blocks = adapter._maybe_blocks
    orig_post_chunks = adapter._post_chunks
    orig_edit_message = adapter.edit_message
    # 新契约（b3059921bc 起）才有 _commit_stream；老树（如 836b5f8 部署档）没有。
    # 取不到就不包这个出口（wrap 其余部分照常），绝不让整个包装炸掉下线。
    orig_commit_stream = getattr(adapter, "_commit_stream", None)
    # 老树的收尾出口（836 档）；_wrap_836_finalize 在尾部按需接线。
    _try_finalize_stream_orig = getattr(adapter, "_try_finalize_stream", None)

    # 目标工作区 id 的单一解析链：显式 team_id > metadata keys > chat 映射。
    def _target_team_id(chat_id=None, team_id=None, metadata=None):
        return team_id or _metadata_team_id(metadata) or _team_key(adapter, chat_id)

    def _maybe_blocks_patched(content):
        # 必须保持同步签名（树内调用点同步取值）。
        #
        # v1.3 r4（评审 #1）：树内 _maybe_blocks(content) 只有正文、无工作区
        # 上下文，「唯一缓存表」不能证明目标工作区——A/B 双工作区只预热 B
        # 时，上游 `_get_client` 的 primary fallback 会把消息发去 A 的客户端，
        # 此时若拿唯一幸存的 B 表解析，text 保留 @name 而 blocks 换成 B 的
        # 用户 ID，同一条消息两处指人不一致。目标不明 → text 与 blocks 都
        # 保留原文。直通窗口（contextvar，仅当前调用上下文可见）内同样不
        # 解析：载荷已由 _commit_stream 包装精确构造。
        try:
            if _stream_edit_ctx.get() is None:
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

    async def _resolve_async_outlet(content, chat_id=None, team_id=None,
                                    metadata=None, floor=None):
        """async 出口统一解析：先确保表就绪（冷启动首条也解析），再替换。

        team 路由与树内出站一致：显式 team_id（_post_chunks 自带）>
        metadata 工作区（edit 的 _client_for 同源 keys）> chat 映射 > ""。
        链路解析不出 team 且适配器认证了**多个**工作区时保留原文——
        `_get_client` 的「取第一个 client」fallback 对发消息无害，对
        名字→ID 是拿 A 的表猜 B（round 3 #1 的 async 侧同型洞）。
        单工作区（len==1）用该 team 建表（表键与同步路径一致）。

        ``floor`` 非空时是流式收尾的已发送前缀长度：只改写 floor 之后
        **开始**的提及（_resolve_tail_mentions），前缀字节原样——与
        _commit_stream_patched 同一条铁律（round 3 #2）。
        """
        tid = _target_team_id(chat_id, team_id, metadata)
        if not tid:
            team_clients = getattr(adapter, "_team_clients", None) or {}
            if len(team_clients) > 1:
                return content  # 目标工作区无法确定 → 保留原文
            if len(team_clients) == 1:
                tid = next(iter(team_clients))
        try:
            table = await _name_table(adapter, chat_id=chat_id, team_id=tid)
            if floor is not None:
                return _resolve_tail_mentions(content, table, floor)
            return _build_repl_string(content, table)
        except Exception:
            return content

    async def _post_chunks_patched(chat_id, team_id, content, formatted, thread_ts):
        formatted = await _resolve_async_outlet(formatted, chat_id, team_id)
        content = await _resolve_async_outlet(content, chat_id, team_id)
        return await orig_post_chunks(chat_id, team_id, content, formatted, thread_ts)

    async def _edit_message_patched(chat_id, message_id, content, *,
                                    finalize=False, metadata=None):
        # v1.3 r4（评审 #2）：流收尾的原位 finalize edit 直通判定改为按调用
        # 现场解析目标 (team, channel, ts) 精确匹配，并只认当前调用上下文
        # （contextvar）里登记的流——不再用适配器级全局 set(ts)：
        #   - 不同工作区的同 ts 流并发收尾，A 完成后不再撤销 B 的保护；
        #   - A 收尾等待期间，B 对同 ts 的普通编辑不再被错误跳过提及解析。
        target = _stream_target_key(adapter, chat_id, message_id, metadata)
        try:
            active = _stream_edit_ctx.get()
        except LookupError:
            active = None
        if active is not None and target is not None and target in active:
            # 载荷已由 _commit_stream 包装精确构造（extends → floor 限定尾段
            # 解析；replace → 进入保护上下文**之前**已完成整篇解析），直通
            # 原样送达，绝不再跑全文解析：纯文本部署（rich_blocks=False）下
            # orig _maybe_blocks 恒 None——「没有 blocks」不等于「text 缺解
            # 析」，再解析会把已发送前缀里的 @name 二次改写（round 5 P2：
            # draft="Done @Alice" 的恢复 edit 被写成 "Done <@U9> and <@U9>"）。
            # r4 的 final_blocks 探测补偿即此回归根源，撤销。
            return await orig_edit_message(chat_id, message_id, content,
                                           finalize=finalize, metadata=metadata)
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
        # - delta=""：纯封口（片段切换/清理），无文本可解析，原样透传。
        #   但 blocks 渲染（finalize edit）仍会发生——用 stream ts 打直通标记，
        #   防止 edit_message 包装二次解析全文（round 3 #2 已发送前缀保护）。
        # - extends 不变量：以**上游真身** _stream_relation 为准（round 3 #3），
        #   认「字节前缀」和「去前导空白后对齐 sent.strip()」两档——包装此前
        #   自作主张 text.startswith(sent) 把合法第二档误判断裂。
        team_id = key[0] if isinstance(key, (tuple, list)) and key else None
        if not text:
            return await _pass_through_edit(
                stream, key, orig_commit_stream(
                    key, stream, text, metadata, delta=delta, replace=replace))
        if replace:
            new_text = await _resolve_async_outlet(text, team_id=team_id)
            return await _pass_through_edit(
                stream, key, orig_commit_stream(
                    key, stream, new_text, metadata, delta=delta, replace=replace))
        sent = (stream or {}).get("sent", "")
        if not _stream_extends(adapter, stream, text, delta):
            return await _pass_through_edit(
                stream, key, orig_commit_stream(
                    key, stream, text, metadata, delta=delta, replace=replace))
        # extends 确认：head 字节不动（已发送），只解析 floor 之后开始的提及；
        # 判定（代码围栏/词边界）基于完整终稿（round 3 #2）。
        floor = len(text) - len(delta)
        try:
            table = await _name_table(adapter, team_id=team_id)
            new_text = _resolve_tail_mentions(text, table, floor)
        except Exception:
            new_text = text
        new_delta = new_text[floor:] if (len(new_text) >= floor
                                         and new_text[:floor] == text[:floor]) else delta
        return await _pass_through_edit(
            stream, key, orig_commit_stream(
                key, stream, new_text, metadata, delta=new_delta, replace=replace))

    async def _pass_through_edit(stream, key, awaitable):
        """流收尾的 finalize edit 用我们构造好的精确载荷直通。

        orig 内部 edit_message(chat_id, ts, shown, finalize=True)（新树）或
        ``self._maybe_blocks(text)`` + chat_update（老树 836）都会再进包装的
        同步出口 → 全文解析 → 已发送前缀里的 @name 被二次改写 = Slack 上
        同一条消息 text 与 blocks 不一致（round 3 #2 的复现形状）。

        v1.3 r4（评审 #2）：流身份 = (team, channel, ts)，登记进 contextvar
        ——只对当前调用上下文（task 及其 await 子树）可见，作用域即 orig 调
        用期间。并发流互不干扰：A/B 两工作区的同 ts 流并发收尾，A 先完成不
        会动到 B 的登记；A 等待期间 B 的同 ts 无关普通编辑因不在 A 的上下
        文里照常解析。edit_message 包装按调用现场解析目标键精确命中才直通。
        收尾异常/取消由 finally 撤销登记。嵌套收尾（同流内嵌 _commit_stream）
        时外层登记已覆盖，直接透传，不叠加计数。
        """
        ts = (stream or {}).get("ts")
        ident = _stream_identity(adapter, key, ts)
        if ident is None:
            return await awaitable
        prior = _stream_edit_ctx.get()
        active = set(prior) if prior else set()
        if ident in active:
            return await awaitable  # 嵌套收尾：外层登记已覆盖本流
        active.add(ident)
        token = _stream_edit_ctx.set(frozenset(active))
        try:
            return await awaitable
        finally:
            _stream_edit_ctx.reset(token)

    async def _try_finalize_stream_patched(chat_id, content, *args, **kwargs):
        """老树（836b5f8 部署档）的 send() 顶部收尾出口（v1.4）。

        上游真身（部署树 adapter.py）：
          ``async def _try_finalize_stream(self, chat_id, content)``
          - 在 send() **最前**执行，先于 _post_chunks——v1.3 的 async 预热
            出口在这条路径永不触发，冷表下收尾全是生文本；
          - 仅当 ``text.startswith(sent)`` 且 sent 非空才认领（claim），认领即
            ``_active_streams.pop(chat_id)``，delta 自切 = final_text[len(sent):]；
          - seal 后用 ``self._maybe_blocks(text)`` 渲染 finalize blocks 再
            chat_update——那是包装后的同步出口，需 contextvar 直通保护。

        包装策略（与 _commit_stream_patched 同铁律）：
        1. 不改变认领判定——text 先用 orig 同款 ``_strip_stream_cursor``
           剥游标再比对（只读复刻，不猜格式）；
        2. 认领成立 → 先 await 建表，floor = len(sent) 只解析未流出尾段，
           把解析后的**整篇** final_text 递给 orig：orig 自切的
           markdown_text delta（= final_text[len(sent):]）天然带上已解析尾段；
        3. 全程打 (team, channel, ts) 直通标记：orig 内部的 finalize
           ``self._maybe_blocks``（同步包装）按精确流身份命中，不再全文
           二次解析——纯文本部署下渲染器恒 None，「没有 blocks」≠「text
           缺解析」（round 5 P2），已发送前缀字节绝不重写；
        4. 未认领（interim commentary / 前缀断裂）→ 原样透传，表都不建——
           这条路径上游直接 return None 走 _post_chunks，预热由它负责；
        5. 任何异常兜底：透传 orig 原参数，绝不让收尾炸掉（best-effort 契约）。
        """
        streams = getattr(adapter, "_active_streams", None) or {}
        stream = streams.get(chat_id)
        if stream is None:
            return await _try_finalize_stream_orig(chat_id, content, *args, **kwargs)
        sent = stream.get("sent", "")
        strip = getattr(adapter, "_strip_stream_cursor", None)
        text = strip(content) if callable(strip) else content
        # 认领判定与上游逐字对齐：sent 空 / 前缀不匹配 = interim，不归我们管。
        if not sent or not isinstance(text, str) or not text.startswith(sent):
            return await _try_finalize_stream_orig(chat_id, content, *args, **kwargs)
        ts = stream.get("ts")
        ident = _stream_identity(adapter, chat_id, ts)
        try:
            new_text = await _resolve_async_outlet(
                text, chat_id=chat_id, floor=len(sent))
        except Exception:
            new_text = text
        prior = _stream_edit_ctx.get()
        active = set(prior) if prior else set()
        nested = ident is not None and ident in active
        if not nested and ident is not None:
            active.add(ident)
            token = _stream_edit_ctx.set(frozenset(active))
        else:
            token = None
        try:
            return await _try_finalize_stream_orig(chat_id, new_text, *args, **kwargs)
        finally:
            if token is not None:
                _stream_edit_ctx.reset(token)

    def _wrap_836_finalize():
        """老树档接线（仅在无 _commit_stream 时调用，见 _wrap_adapter 尾部）。"""
        if callable(_try_finalize_stream_orig):
            adapter._try_finalize_stream = _try_finalize_stream_patched

    adapter._maybe_blocks = _maybe_blocks_patched
    adapter._post_chunks = _post_chunks_patched
    adapter.edit_message = _edit_message_patched
    if orig_commit_stream is not None:
        adapter._commit_stream = _commit_stream_patched
    else:
        # 老树档（836b5f8 部署树）：无 _commit_stream，send() 的收尾走
        # _try_finalize_stream。新树上 send() 经 _try_finalize →
        # _commit_stream（已包）——此时**绝不**再包 _try_finalize，否则
        # 尾段被解析两次（_try_finalize 包装先解析一遍，进 orig 后
        # _commit_stream 包装又按 floor 解析一遍，幂等性全靠查表不撞）。
        _wrap_836_finalize()
    LOG.info("[Slack] mention plugin v1.4: adapter wrapped (instance-level)")
    print("[slack-mention-plugin] adapter wrapped v1.4", flush=True)


def _factory(native, adapter):
    """ctx.register_platform_handler("slack", ...) 的 factory。"""
    try:
        _wrap_adapter(adapter)
    except Exception:
        LOG.error("[Slack] mention plugin wrap failed", exc_info=True)


def register(ctx):
    ctx.register_platform_handler("slack", _factory)
