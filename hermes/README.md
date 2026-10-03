# Hermes Agent × Slack（原生平台插件）

本目录是 **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** 的 Slack 接入包。
Hermes 自带一等的 Slack 平台插件（slack-bolt **Socket Mode**，出站 WebSocket，无需公网入口），
**不需要** `muse/`、`grokbot/` 那类自建桥——直接用 Hermes 自己的配置面。

> 给另一个 agent 的完整配置清单见 **[AGENT.md](./AGENT.md)**（整段复制给它即可）。

## 和兄弟目录的差异

| | `muse/` / `grokbot/` | `hermes/` |
|---|---|---|
| 形态 | 自建 bridge.py + consumer 轮询 | Hermes 原生平台插件（gateway 进程内） |
| 触发 | inbox 队列轮询 | 事件直达 agent 回合 |
| 会话/记忆/cron | 自己写 | 全部内置（session、memory、skills、cron、slash 命令） |
| 依赖 | slack_sdk | Hermes 本体（`pip install slack-bolt` 随安装器带上） |

## 架构

```
Slack ──DM/@mention/斜杠命令──▶ Slack App（Socket Mode）
        ──出站 WebSocket──▶ hermes gateway（本机/服务器）
        ──▶ agent 回合（工具/记忆/skill）──▶ chat.postMessage 回 Slack
```

## 快速开始

### 0. 安装 Hermes

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

### 1. 建 Slack App（manifest 一把梭）

manifest 由 Hermes 自带命令生成（含全部内置 slash 命令、bot scope、事件订阅——Hermes 升级后重跑即得新版，无需依赖本仓库）：

```bash
hermes slack manifest --agent-view --write   # 写到 ~/.hermes/slack-manifest.json
```

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest**
2. 选 workspace，粘贴生成的 manifest 全文 → **Create**
3. **Settings → Socket Mode** 应已开启（manifest 自带）；Basic Information → **App-Level Tokens** → Generate（scope 含 `connections:write`）→ 复制 `xapp-` token
4. **Settings → Install App** → Install to Workspace → 复制 Bot User OAuth Token（`xoxb-`）
5. manifest 里 messages tab 已开，无需再手动启用

> Hermes 升级新增 slash 命令后：重跑上面命令，到 App Manifest 页粘贴更新，Slack 要求时重装。

### 2. 配置 Hermes

token 与授权走 `~/.hermes/.env`（参考 [.env.example](./.env.example)）：

```bash
SLACK_BOT_TOKEN=xoxb-…          # Bot Token
SLACK_APP_TOKEN=xapp-…          # App-Level Token（connections:write）
SLACK_ALLOWED_USERS=U01ABC2DEF3 # 授权 Member ID，逗号分隔（人类消息的前提，见「授权模型」）
SLACK_HOME_CHANNEL=C01234567890 # 可选：cron/通知默认频道
```

行为走 `~/.hermes/config.yaml`（参考 [config.example.yaml](./config.example.yaml)）：

```yaml
platforms:
  slack:
    enabled: true
    home_channel: {platform: slack, chat_id: C01234567890, name: general}
    extra:
      reply_in_thread: false   # 频道顶层直接回复；true=开 thread
      allow_bots: mentions     # 多 agent 协作推荐值（见下）
      rich_blocks: true        # Block Kit 富渲染（表格/嵌套列表）
```

或直接交互式向导：`hermes gateway setup` → 选 Slack。

### 3. 启动

```bash
hermes gateway            # 前台跑一次看日志
hermes gateway install    # 装成 systemd user service（开机自启）
hermes gateway status     # 看连接状态
```

### 4. 验收

- Slack 里 `/invite @Hermes Agent` 进目标频道
- DM 直接说话；频道里 `@Hermes Agent 你好`
- 终端能跑 `hermes status` / `hermes gateway status` 看到 slack adapter connected

## @ 方式（触发模型）

| 场景 | 触发 | 回复位置 |
|---|---|---|
| 1:1 DM | 无需 @，每条都回 | DM |
| 频道顶层 | 必须 `@bot`（`require_mention: true` 默认） | `reply_in_thread: false` → 频道顶层；`true` → thread |
| thread 内 | 首次 @后，同 thread 后续免 @自动跟随 | 原 thread |
| 其他 bot（经典 bot 帖：`bot_id`、无 `user_id`） | `allow_bots: mentions` 时，对方消息里明确 @ 了本 bot 才受理 | 同上 |
| 其他 bot（带 `user_id` 的 app/automation 帖） | 上者之外，**其 user_id 还必须通过用户授权**（`SLACK_ALLOWED_USERS` 等），否则静默拒绝 | 同上 |
| 斜杠命令 | `/new` `/model` `/help` …（50 个，原生注册） | ephemeral（仅自己可见） |
| thread 里的命令 | Slack 禁止 thread 内斜杠，用 `!cmd` 前缀（`!queue`、`!stop`） | thread 内 |

细粒度开关：`strict_mention`（每条都要 @，禁自动跟随）、`thread_require_mention`（thread 内强制 @）、`free_response_channels`（免 @ 频道白名单）、`mention_patterns`（自定义触发词）。

## 多 agent 协作（防回环）

本仓库的三 bot 同频道实测配置（Hermes 视角）：

```yaml
platforms:
  slack:
    extra:
      allow_bots: mentions   # 对方 bot 消息里明确 @ 我才受理
      reply_in_thread: false
```

配合顶层 `slack.strict_mention: true` 更严。铁律：

- `mentions` 是协作起点；需要对方接手时明确点名。每一步最多一条实质终稿，发完结束该回合等新事件，必要接手与结果返回可以继续有界多轮。
- 避免 `allow_bots: all`（两个互相全收的 bot 会无限对话）
- Hermes 永远忽略自己的消息（防自回环，无法关闭）；网关另有 bot loop guard（每会话滑动窗口超额冷却）兜底
- 对方 bot 引用旧消息时把 @ 转纯文本名字，防二手点名触发回环
- `allow_bots` 管「bot 消息受不受理」，**不是身份白名单也非绝对防回环保证**——授权全貌见下节

## 授权模型（以源码为准）

> 以下按 hermes-agent commit `b3059921bc`（origin/main，2026-10-02）核对；`9a71d5a8..b3059921` 期间
> `gateway/authz_mixin.py` 与 `plugins/platforms/slack/adapter.py` 的授权路径零改动。
> 入站链（按源码实际顺序）：`adapter._prefilter_inbound` 内 dedup → ignored-channel →
> **`_drop_bot_sender`（own-echo 丢弃 + `allow_bots` gate）** → **`_early_reject_unauthorized`**
> （仅当事件带 `user_id` 才运行；签名只有 `(user_id, channel_id, is_dm)`，**不带** `is_bot`，
> 且对 `_is_sender_authorized` 的调用也不传 `is_bot` → 该 user_id 按人类路径裁决）→
> MessageEvent 构建时才置 `is_bot=_event_declares_bot_sender(event)` → 网关入口
> `_is_user_authorized`（`_chat_scoped_grant` 内 `{PLATFORM}_ALLOW_BOTS` 短路 → allow-all →
> pairing → allowlist → default-deny）→ bot loop guard。

**三件套分工，别混为一谈：**

1. **用户授权**（谁能说话）：`SLACK_ALLOWED_USERS`（逗号分隔 Member ID）是主开关；辅助有
   `SLACK_ALLOW_ALL_USERS=true`、`GATEWAY_ALLOWED_USERS`、pairing 配对码、`GATEWAY_ALLOW_ALL_USERS`。
   - 人类消息：不设任何 allowlist/pairing → **默认拒收**（fail-closed）。没配白名单不等于全员放行。
   - 带法：Member ID（`U…`）精确匹配；填邮箱/用户名无效。

2. **bot 触发条件**（bot 消息额外过的一道 gate，`allow_bots`）：
   - `none`（默认）：所有 bot 帖丢弃。
   - `mentions`：对方 bot 的消息**明确 @ 本 bot** 才受理。
   - `all`：全部受理（仅靠 own-echo 丢弃 + bot loop guard 兜底，不推荐）。
   - **关键分叉**——对方 bot 的帖子里带不带 `user_id`：
     - **经典 bot 帖**（`bot_id`/`bot_profile`、**无** `user_id`，含 Slack Workflow Builder 无 user 的帖子）：
       `_chat_scoped_grant` 里 `{PLATFORM}_ALLOW_BOTS` 短路在 no-user-id guard **之前**生效——
       即 `allow_bots: mentions` + 明确 @ 就能进，**无需**把它加进 `SLACK_ALLOWED_USERS`。
     - **带 `user_id` 的 bot/app/automation 帖**（app 发的且带 user 字段、`app_id`+无 `client_msg_id` 签名）：
       这类帖子**先**过 adapter 的 `allow_bots` gate（`_drop_bot_sender`），**再**吃早期授权检查——
       检查里 `user_id` 必须本身通过用户授权（allowlist/pairing/allow-all），否则被静默拒绝；
       早期检查**不带** `is_bot`，所以这里的 ALLOW_BOTS 短路帮不上忙。想接这类发送者：把它的
       user_id 加进 `SLACK_ALLOWED_USERS`，且 `allow_bots` 不能是 `none`。
   - `allow_bots` 不是身份白名单：它只决定「bot 签名的消息要不要受理」，身份与授权始终由上面的用户授权层裁决。

3. **频道范围**（在哪响应）：`allowed_channels`（只在这些频道响应，DM 豁免）、`free_response_channels`
   （免 @ 白名单）、`ignore_other_user_mentions`、`strict_mention`/`thread_require_mention`。
   授权≠响应：**已授权**用户在非 `allowed_channels` 频道发消息，bot 也会静默不响应。
   事件订阅面另有限制：`app_mention` 只在 bot 已加入的频道/DM 生效（自身家 bot 例外）。

**独立验证**（`hermes/test_authz_matrix.py`：固定 commit 临时 worktree 加载真 SlackAdapter，upstream
smoke-test 同款 mock，12/12 断言；源码行级顺序另由 `hermes/verify_authz_order.sh` 钉住）：

| # | 场景 | allowlist | allow_bots | @本bot | 结果 |
|---|---|---|---|---|---|
| A | 人类、无任何 allowlist | 无 | — | — | 拒（fail-closed） |
| B | 无 user_id 的 bot 帖 | 无 | mentions | 是 | **过**（ALLOW_BOTS 短路） |
| C | 带 user_id 的 bot 帖 | 无 | mentions | 是 | 拒（早期检查 user_id 未授权） |
| D | 经典 bot 帖 | 有(人类) | mentions | 是 | 过 |
| E | 经典 bot 帖 | 有(人类) | none | 是 | 拒（adapter gate） |
| F | 本 bot 自己的帖子 | 有(人类) | all | — | 拒（own-echo） |
| G | 经典 bot 帖、无 @ | 有(人类) | mentions | 否 | 拒（adapter gate） |

> 上游自带的同类回归：`tests/gateway/test_discord_bot_auth_bypass.py`（#4466：`DISCORD_ALLOW_BOTS` 无
> allowlist 也放行 is_bot source）、`tests/gateway/test_slack_peer_agent_smoke.py`（peer-agent 路由不变量）。

## slack-mention 插件（出站 @提及）

本目录的 [slack-mention/](./slack-mention/) 是**自建 owner 插件**（非 Hermes 官方插件），
在本机以 `~/.hermes/plugins/slack-mention/` 运行、随备份链路同步。功能：

- 出站文本里的 `@显示名/@用户名` 自动解析成 `<@U…>` 真提及（users_list 全量缓存 10min）
- `<@U…>/<#C…>/<!here>` 等实体转原生 rich_text mention 元素（加粗提及也继承 style）
- 零 core 改动：`register_platform_handler("slack", factory)` 实例级包装 adapter

**血坑（务必保持）**：树内 `adapter._maybe_blocks` 是**同步**方法，插件包装必须保持同步签名
（async 化会让调用点拿到 coroutine 塞进 `chat.postMessage` → `not JSON serializable` →
Slack 出站全灭，10-03 凌晨实测 667 次报错）。名字解析只用缓存表，表由 `_post_chunks`
的 async 路径预热。启用：`plugins.enabled` 列 `slack-mention`。

**v1.3（PR #10 三项复核）**：流式收尾包装从 `_seal_stream` 换到 `_commit_stream`
（上游 `a5e7df27c7` / `b3059921bc` 真实契约：key=(team,chat,thread_ts)、
`delta=`=未流出尾段、stopStream APPEND 语义——解析后的尾段同时改写 text 尾部与
delta 本身；`replace=True` 走 chat.update 整篇解析）。`users_list` 响应是
`AsyncSlackResponse`：支持 `.get()` 但不是 dict——鸭子型取值，两页分页都能进表。
同步取表严格绑目标工作区：显式 team_id > metadata keys > chat 映射；无上下文时
只有唯一 team 键才用（多 team 键 = 保留原文，绝不幸存者偏差指错人）。
测试 `slack-mention/test_slack_mention_v13.py`：固定 commit worktree 载真实上游，
37 断言（契约/分页/全链路 send_draft→send→_commit_stream/多工作区隔离/歧义）
——`HERMES_ROOT` 环境变量指 checkout，可选 argv 传 repo/commit。

**v1.4（部署树 836b5f8 契约对齐）**：部署树**没有** `_commit_stream/_stream_key/
_stream_relation`——文本收尾走 `send() → _try_finalize_stream(chat_id, content) →
_seal_stream(chat_id, stream, final_text=, blocks=)`，且在 `send()` 顶部**先于**
`_post_chunks` 执行，流身份是 `_active_streams` 的 **chat_id 索引**（非三元组
key）。v1.3 在该树上收尾出口全空转：stopStream 的未流出尾段与 finalize
chat.update 的 blocks 全是生文本（冷缓存 mention 不解析）。v1.4 在
`_commit_stream` 取不到时改包 `_try_finalize_stream`（新树绝不双包）：认领判定
逐字对齐上游（`_strip_stream_cursor` + `startswith(sent)`），floor=len(sent) 只
解析未流出尾段，解析后的整篇 final_text 递给 orig（orig 自切 markdown_text
delta）。传入 orig 前加回原始游标/空白后缀，由 orig 去掉原游标一次；避免正文
末尾省略号被二次剥除、claim 翻转而另发整篇。contextvar 直通窗口保护 orig 内部 `self._maybe_blocks` 的 finalize
渲染不二次全文解析（已发送前缀字节绝不重写）。老树 extends = 字节前缀语义
（`len(final_text) > len(sent)` 才带 markdown_text，相等 = 纯封口）。
测试 `slack-mention/test_slack_mention_v14.py`：同款真实上游方法（固定
836b5f8253 worktree），19 断言（契约 A1-A5 / 冷缓存全链路 T1 / 富文本收尾
T2 / 片段切换 T3 / 前缀断裂 T4 / edit 普通路径 T5 / 幂等+team 隔离 T6 /
游标与正文省略号、空白、claim 保持和单流封口 T7）；
v13 双套件（37+39 断言）在新树 a5e7df27c7 上回归全绿。

启动日志会给出实际流出口 `commit_stream` 或 `try_finalize_stream_836`；
两者均缺失时明确警告 `MISSING`，只说明普通发送/编辑包装可用，不声称流收尾就绪。

### 有界协作的 Slack 展示配置（836b5f8）

如果需要同线程单次终稿、避免工具进度刷屏，可在备份现有配置后合并以下字段：

```yaml
display:
  platforms:
    slack:
      streaming: false
      interim_assistant_messages: false
      thinking_progress: false
      tool_progress: "off"
      live_status: "off"
      long_running_notifications: false
      show_reasoning: false
platforms:
  slack:
    typing_indicator: false
    extra:
      reply_in_thread: false
```

这些显示字段只覆盖 Slack；不覆盖现有 `allow_bots`、作者/频道授权或其他平台。
`reply_in_thread: false` 避免给普通私聊或频道顶层消息强制新建线程；已有的
`thread_ts` 仍沿用原线程。836 适配器的此开关不区分私聊与频道，设为 `true`
也会把普通私聊回复放进线程，不能作为仅控制频道展示的设置。依据固定上游
[入站线程识别](https://github.com/NousResearch/hermes-agent/blob/836b5f8253d27fee79b4f833bc43624f06a890b3/gateway/platforms/slack.py#L4148-L4172)与
[出站线程解析](https://github.com/NousResearch/hermes-agent/blob/836b5f8253d27fee79b4f833bc43624f06a890b3/gateway/platforms/slack.py#L2757-L2762)。
这也会让频道顶层消息使用频道级会话，而非每条根消息各建会话；作者隔离仍由
既有 `group_sessions_per_user` 控制。DM 会话隔离另由
`dm_top_level_threads_as_sessions` 控制，不随回复展示位置自动改变；本配置不修改这些字段。
显式 `tool_progress: "off"` 也关闭 native task cards。审批仍走独立
`send_exec_approval` 路径，不能为了静默输出关闭审批。依据固定上游
[展示解析](https://github.com/NousResearch/hermes-agent/blob/836b5f8253d27fee79b4f833bc43624f06a890b3/gateway/display_config.py)、
[回合显示](https://github.com/NousResearch/hermes-agent/blob/836b5f8253d27fee79b4f833bc43624f06a890b3/gateway/run_turn.py)与
[回合执行/审批](https://github.com/NousResearch/hermes-agent/blob/836b5f8253d27fee79b4f833bc43624f06a890b3/gateway/run_turn_runner.py)。

使用 adapter 自动终稿作为正文的唯一发送入口；同一回复不要再经 curl 或发送工具另发。
普通协作每一步最多发送一条实质终稿，发送后立即结束该回合，等待新的 Slack 事件回来。
需要接手、澄清或结果返回时可继续下一步骤的有界回合；不要在 terminal 中 `sleep` 长轮询等待其他 agent，
也不要用 raw curl `chat.postMessage` 中途另发正文。这样后续事件能在正常回合里读取新状态，
每一步的实质发言都有对应的 assistant 历史和原生发送记录。安全审批继续使用原入口。
这些设置不屏蔽所有诊断和 memory 更新，也不改变超长文本切块行为，不能当作 exactly-once
保证。普通短回复的条数、线程和完成即停止仍须真实验收；磁盘文件更新后还须核对新进程加载。
需要加载配置时，可由已授权用户通过 Hermes 原生 `/restart` 请求正常重启；固定 836
[命令处理器](https://github.com/NousResearch/hermes-agent/blob/836b5f8253d27fee79b4f833bc43624f06a890b3/gateway/slash_commands.py#L524-L582)
会先等待运行中的任务结束。终端工具禁止在 agent 子进程中自行杀死 gateway 的保护，
不能推广成所有原生重启入口均不可用。重启后仍须核对新启动时间、加载配置和一条真实回复。

### 836 的后台复盘通知

J 实测发现，终稿之后的 `Self-improvement review` 仍会单独发进 Slack。
固定 836 的 `run_turn_runner.py` 直接读取全局 `display.memory_notifications`，
上述 Slack 显示开关不控制它。后台复盘可能继续更新记忆或技能；主回合的只读报告
不能代表整个实例没有写入。学习流程、运行日志与安全审批是不同的控制面。

本仓库提供针对 `836b5f8253d27fee79b4f833bc43624f06a890b3` 的
[最小补丁](./patches/memory-notifications-platform-836.patch)：为该显示键登记平台覆盖，
并让真实回合装配通过已有 resolver 读取当前平台值。只改两个上游文件，不改后台学习、
审批或诊断通知；其他平台继续继承原全局值。加载补丁后可额外设置：

```yaml
display:
  platforms:
    slack:
      memory_notifications: "off"
```

不要只在未补丁的 836 配置中添加该键并宣称生效。应用前检查实际上游版本、脏改并备份
两个目标文件和配置；先 `git apply --check`，出现冲突就停止，不能强制覆盖已有修改。
正常加载后用实际 Slack 回复验证。上游正式支持这一覆盖后，按实际调用链复核并撤下本地补丁，
不要跨版本盲目重复应用。

离线回归读取**已应用补丁**的真实上游目录，不写入该目录：

```bash
python3 hermes/test_memory_notifications_platform.py /path/to/patched/hermes-agent
```

测试编译两个实际文件并执行原 resolver 与 AST 提取的完整回合装配方法，覆盖 Slack 覆盖、
其他平台继承、布尔/空值及复用 agent 的下一回合恢复。无关回调使用隔离替身；
这不等于完整 gateway 或真实 Slack 通知验收，部署后必须另验。

## 读取历史

固定 836 在真实 thread 回复里自动补 `conversations.replies` 上下文；频道顶层不会自动补频道历史。
`reply_in_thread: false` 的默认会话键为
`agent:main:slack:group:<team>:<channel>:<sender>`，不同作者的对话历史隔离。
真实 thread 默认不追加 sender，仍由 `thread_sessions_per_user` 控制。消息可以进入 Hermes，
却不因此拥有其他作者的开局、报名或自己经 raw curl 发出的正文；不能把触发成功当作协作连续性。

需要补频道级上下文（多 agent 对账、审计）时用本目录 CLI：

```bash
# 频道最近 20 条（正序 JSON lines；token 自动从 env 或 ~/.hermes/.env 取）
python3 channel_history.py C01234567890 20
# 某条消息的 thread（limit 放 --thread 前后都行）
python3 channel_history.py C01234567890 --thread 1234567890.123456
python3 channel_history.py C01234567890 --thread 1234567890.123456 10
# 顺带把 user ID 解析成显示名
python3 channel_history.py C01234567890 20 --resolve
# 补读当前观察窗口之前的频道消息；不含边界消息，末尾显示 has_more/next_cursor
python3 channel_history.py C01234567890 100 --before-ts 1234567890.123456 --page-info
```

`--before-ts` 仅用于频道，不能与 `--thread` 同用；`--page-info` 是可选元数据，默认仍只输出消息行。
对 `has_more`、上下文 gap、未读取的 thread 或已省略消息，先补查再断言谁没有报名/是否开过局。
脚本凭据必须属于目标 workspace；多 workspace 自动观察则由候选补丁使用精确 team 客户端路由。

### 836 的自然频道协作候选

[候选补丁](./patches/natural-collaboration-836.patch) 基于固定
`836b5f8253d27fee79b4f833bc43624f06a890b3`，在上述 memory-notifications 补丁之后顺序应用。
增加现有 Slack adapter 的观察方法、在普通/排队回合共用的 `run_inbound.py`
预处理末尾调用它、为现有 busy ACK 显示判断增加平台键，并在 `run_turn.py` 共用静默权限判定。
没有新服务、依赖或共享会话。
补丁、配置和离线检查是候选交付，尚不代表运行进程已加载或真实多轮协作通过。

```yaml
platforms:
  slack:
    extra:
      collaboration_channels:
        "T01234567890:C01234567890":
          max_messages: 100
          max_chars: 40000
display:
  platforms:
    slack:
      busy_ack_enabled: false
```

使用明确的 `team_id:channel_id` 白名单，无需为每个新任务配置根消息时间戳。
每个实际准备的频道顶层回合读取一次最新公共频道窗口；排队的旧消息也读取执行时的新状态。
一次 `conversations.history` 调用最多请求 `max_messages + 1` 条，15 秒超时，不自动重试或无限翻页。
`max_messages` 范围 1–200，`max_chars` 范围 1–100000；默认分别 100 和 40000。
使用已有 workspace 客户端和缓存名字，每条标注作者 ID、名字、ts、自身身份及信任标记。
名字和正文压成安全的单行，正文按整条保留；不增加用户/频道权限，也不把历史内容当作新指令。
观察在 `@file` 引用解析之后注入，历史引用不会触发本地文件读取。

窗口注明 `fetched_at`、触发作者和消息 ts，时间可晚于触发消息；真实 thread 继续走原水位补史。
频道窗口列出根消息的 `thread_replies` 数量，thread 正文须另读。
API 失败、分页未读完、无效配置、消息数或体积上限都会显示 `[Context gap]`，列出省略的消息 ts、
最早读取 ts 和继续读取参数；原请求仍交给 agent。窗口不是完整频道档案，历史较长时根任务可能在窗口外。
不能根据窗口缺失宣称没有开局或没有报名；重要状态结论发前补查最新消息及必要旧页/thread。

`busy_ack_enabled: false` 只关闭 Slack 忙碌回显；同一分支之前的输入授权、审批、排队、steer 和 interrupt
仍运行，其他平台继承 enabled 默认。既有全局 `HERMES_GATEWAY_BUSY_ACK_ENABLED=false` 仍优先禁止回显；
此键不关闭网关重启/失败诊断或审批提示。

固定 836 原来只允许 machinery 回合的成功 `[SILENT]`；普通 peer bot 的静默也会被替换成可见警告。
候选额外允许：明确配置的 `team_id:channel_id` 内、经过既有用户授权、`source.is_bot=true` 的 Slack group
成功回合，可在没有实质工作时选择精确 `[SILENT]`，结束该回合而不发送正文。
普通和 queued 首段共用同一判定，链末使用实际 terminal 作者，不能借 bot 开局隐藏人类的后续请求。
人类、DM、其他平台、未配置范围和失败回合保持原可见行为；空回复不等于静默。
它不把 peer event 改成 internal，不绕过授权，不迁移审批入口；成功 marker 仍保存到 transcript，
只抑制出站正文，不伪造已完成的发送记录。

正常终稿由 adapter 自动发送并写入当前作者的历史。固定版本的 `send_message` 在发送成功后会尝试
`mirror_to_session`，带当前 `HERMES_SESSION_USER_ID`；这是尽力而为的发送工具镜像，不能保证每个作者都获得它。
raw curl 完全绕过该镜像，自己的 Slack 入站消息又被防回环过滤，不能作为连续 assistant 历史来源。

应用前核对版本和脏改，备份五个目标文件及配置；保留其他既有脏改，不重置或覆盖。
先检查，再顺序应用，冲突即停。现有 PR17 修改已在部署时，
仅检查/应用第二个补丁，不重复应用第一个：

```bash
git apply --check /path/to/hermes/patches/natural-collaboration-836.patch
git apply /path/to/hermes/patches/natural-collaboration-836.patch
python3 /path/to/hermes/test_natural_collaboration.py /path/to/patched/hermes-agent
python3 /path/to/hermes/test_memory_notifications_platform.py /path/to/patched/hermes-agent
```

新回归只读已补丁的五个真实源文件，编译并执行 AST 提取的完整方法，覆盖跨作者开局/报名/自己的既有发言、
workspace 白名单、回合执行时刷新、历史引用隔离、失败/截断、thread、会话键和 busy ACK/审批/输入行为。
另外执行真实静默、queued 首段和递归 terminal 作者路径，验证限定 bot 静默、失败/人类回退和静默记录的持久化。
可传 `--baseline-turn /path/to/original-836/gateway/run_turn.py` 对照原版两个可见 fallback。
传输、媒体和运行时 owner 使用隔离替身，不是完整 gateway 测试。部署及重启须另由已授权 owner 执行，
核对真实加载，再用无需预先配置根 ts 的新任务验收多轮协作；当前状态为 `NOT_EXERCISED`。

### 每轮协作规则的运行时加载

频道观察补丁提供数据，仓库 `AGENT.md` 或 skill 更新不保证既有作者会话看到了操作规则。
固定 836 的绑定 skill 只在新会话自动加载。本目录的
[增量补丁](./patches/collaboration-turn-contract-836.patch) 依赖固定 836 + PR17 + PR19，
只修改 Slack adapter 的现有 `_channel_prompt_with_identity` 与事件装配调用：
仅明确 `group`、非空 workspace、匹配 `collaboration_channels` 精确 `team:channel` 且配置项为 mapping
时，在原 identity 和 channel prompt 之后追加短静态规则。DM、范围外及未分类调用保持原 prompt。

规则通过已有 `channel_prompt` 进入 trusted ephemeral system prompt；公共观察仍留在数据路径，
不增加作者/工具权限。每个新入站事件都携带规则，queued 回合沿用该事件自己的 prompt；
runner 每次执行都重新合并，并把完整 prompt 纳入 agent cache signature，下一事件无需重置已有会话。
规则不追溯修改已经运行或此前已创建的事件；正常加载新进程后须核对源码 hash 和真实回复。

普通频道回复正文只走 adapter final，不在回合中用工具或 API 另发正文；每一步正常 final 后
结束并等待新事件。需要时立即用已安装 history helper 补读，禁止用
sleep、cron、延迟命令或 curl 轮询等候同伴。版本依据最新可见 ts 对账，已解决的旧 bot trigger
按既有权限静默，未收回复保持 pending；旧版表态不能当作新版接受，不能凭沉默或自行冻结期
宣称共同通过，须遵守实际任务的共识规则。修订先说变化点，实际定稿才提供全文。
只原生点名实际接收请求的人。它保留必要的实质多轮讨论、会话隔离和安全审批。
该指导能减少长回合与旧稿，不提供频道全局串行或模型遵守规则的保证。

```bash
git apply --check /path/to/hermes/patches/collaboration-turn-contract-836.patch
git apply /path/to/hermes/patches/collaboration-turn-contract-836.patch
python3 /path/to/hermes/test_collaboration_turn_contract.py /path/to/patched/hermes-agent
```

应用前核对版本、备份 adapter 并保留已有脏改；冲突即停，不重复应用前置补丁。
回归执行真实 prompt/事件/runner/queued 方法，覆盖 scope、DM、原 prompt 顺序及作者归属；
transport/runtime 使用隔离替身，不等于已加载、模型遵守或真实并发协作通过。

排序依据（Slack 官方文档）：`conversations.history` 最新在前，脚本反转为旧→新；
`conversations.replies` 本身旧→新（父消息开头），脚本不再二次反转——两种模式输出统一为时间正序。

失败输出一行结构化 `{"error": {"kind", "detail", …}}` 并退出码 1——`kind` ∈ `missing_token /
http_429 / timeout / connection_failed / connection_reset / incomplete_read / http_protocol_error /
invalid_json / slack_api_error`；429 带 `retry_after`（读 Slack 的 `Retry-After` 头，秒）。不自动重试，
调用方按 `retry_after` 自行调度。如实报告，不编造。响应体读取边界（连接中途重置/截断/坏状态行）同样
结构化报错，不再以未捕获异常退出。`--resolve` 的名称解析是可选增强：任何失败（含 `users.info` 对
bot ID 返回 `user_not_found`、网络错）只降级保留原 ID，绝不中止主读取；`B…` 前缀的 bot ID 走
`bots.info`，`U…/W…` 走 `users.info`。需要更深的历史回放/搜索走 Hermes 的 `session_search` 工具或
Slack SDK。

回归测试：`python3 test_channel_history.py`（35 项断言，无网络、无真实 token、无真实 `~/.hermes`）；
授权顺序声明的源码核验：`./verify_authz_order.sh [repo] [commit]`（默认 `b3059921bc`，需本地
hermes-agent checkout，不联网）。

## Cron / 定时任务

Hermes cron 原生支持 Slack 投递：

```bash
# 对话里直接说或命令行：
hermes cron add "every 1h" "总结一下最新动态" --deliver slack
hermes cron add "in 30m" "提醒我上线" --deliver slack:C0123456789   # 指定频道
hermes cron add "every 2h" "盯价格" --deliver slack:U0123456789    # 直投某人 DM
```

- 默认 `deliver: origin`（谁创建投回哪）；`"slack"` = home channel（`SLACK_HOME_CHANNEL` 或 `platforms.slack.home_channel`）
- cron 输出里的 `MEDIA:/path/file` 会作为原生 Slack 文件分享上传
- 投递不依赖 gateway 在线（standalone sender 用 `SLACK_BOT_TOKEN` 兜底）
- 管理命令：`hermes cron list` / `edit` / `remove` / `pause` / `resume`

## 排障速查

| 症状 | 解法 |
|---|---|
| DM 能回、频道不回 | 加 `message.channels`/`message.groups` 事件订阅 + `channels:history`/`groups:history` scope → **重装 App** |
| 收不到附件 | 加 `files:read` scope → 重装 |
| "发送消息已关闭" | App Home → Messages Tab 开启 |
| 群发 DM（MPIM）没反应 | 加 `message.mpim` 事件 + `mpim:history`/`mpim:read` scope → 重装 |
| 改了 scope/事件没生效 | **必须重装 App**（Install App 页会提示） |
| 两个 bot 打乒乓 | 双方都 `allow_bots: mentions`（绝不 `all`）；仍有兜底：网关 bot loop guard 对超额会话冷却 |
| token 疑似泄露 | Slack 后台 rotate + 更新 `~/.hermes/.env`，聊天记录里出现过的立即作废 |

## 安全

- token 只存 `~/.hermes/.env`（0600）；**永不提交 git、永不贴进聊天**
- 本仓库零凭据：清单里的 token 都是占位符
- `SLACK_ALLOWED_USERS` 必设：不设时人类消息默认拒收（fail-closed）；无 `user_id` 的 bot 帖在
  `allow_bots: mentions` + 明确 @ 下仍可进入（详见「授权模型」）——「不设白名单=拒收一切」
  只对人类消息成立，别当成绝对承诺
- Socket Mode = 纯出站连接，不暴露公网入口
- 提交前自检：`git grep -E 'xoxb-[0-9]|xapp-[0-9]'` 应无真实 token

## 文件树

```
hermes/
├── README.md                    # 本文档
├── AGENT.md                     # 给任意 agent 的端到端配置指令（整段复制）
├── config.example.yaml          # ~/.hermes/config.yaml 的 platforms.slack 片段
├── .env.example                 # token 模板（真实值永不进仓）
├── channel_history.py           # 频道/线程历史 CLI（stdlib-only，零依赖）
├── test_channel_history.py      # channel_history 回归测试（runpy 进程内，35 项断言，零网络）
├── test_authz_matrix.py         # 授权矩阵回归（临时 worktree @b3059921bc，12 项断言）
├── test_memory_notifications_platform.py # 已补丁上游的只读平台通知回归
├── test_natural_collaboration.py # 已补丁 836 的频道上下文/忙碌回显隔离回归
├── test_collaboration_turn_contract.py # 每轮 trusted 协作规则与 scope/queued 回归
├── patches/                    # 固定 836 的平台通知、观察与每轮规则增量补丁
├── verify_authz_order.sh        # 授权模型源码顺序核验（固定 commit，零网络）
└── slack-mention/               # 出站 @提及 插件（本机 owner 自建，见下节）
```
