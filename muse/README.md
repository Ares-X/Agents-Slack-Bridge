# Muse / Generic Agent Slack Bridge

本目录是 **Muse / 任意带 `generate_reply()` LLM 钩子的 agent** 用的 Slack 桥接包。Socket Mode 出站长连接，无需公网入口。

> 仓库根目录还有 `grokbot/`（默认允许多 agent）、`hermes/`（原生插件）。请先 `git clone` 根仓库，再 `cd muse`。

### 多 agent 频道协作（默认开启）

muse 桥**默认放行其他 bot 的 @mention**（自己的消息永远过滤，防自循环）——与 grokbot 行为一致，装完即可同频道互相 @、读上下文、协作：

1. **互相 @**：默认无需配置。要收紧成白名单，在 `.env` 填 `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS`（对方 U…/B…，逗号分隔；任一非空即白名单模式）。
2. **每次回复前读最新原文**：`channel_history.py <channel> [N]`；线程另读 `--thread-ts <ts> --all`。需要更早上下文时用 `--all` 或 stderr 返回的 `next_cursor` 配合 `--cursor` 继续读取。正文不截断，保留身份、线程、全文哈希、发送关联 ID 和控制卡结构；失败退出 2，不能把失败当空历史或只凭摘要判断。
3. **回复位置跟随上下文**：在 thread 里被 @ 就在同一 thread 回（传 `--thread-ts`）；新起话题才发顶层。不要为了"让同伴看见"一律改顶层——会破坏 thread 上下文。
4. **自然协作**：先理解任务与最新上下文；同一任务可持续多轮，对同一来源消息至多一条实质回复，且不是每条消息都必须回复。纯 ACK、重复状态、控制卡没有新行动时用带原因的静默完成；开放讨论、游戏动作、需要下一位继续的问答也属于真实交接，交接时显式 `--mention <UID>`。不要 @ 自己；转述名字用纯文本，无交接不传 mention。

---

## 1. 架构

```
Slack ──① 私信/@mention──▶ Slack App ──② Socket Mode 事件推送（出站 websocket，秒级）──▶
bridge.py ──③ 写入 inbox.jsonl（本地队列）──▶ hook / side chat ──④ agent 生成回复或静默决定 ──▶
send_durable.py ──⑤ 持久化领取/发送/确认 ──▶ Slack
```

延迟构成：①→③ 实时；④→⑤ 取决于外部 hook 的轮询间隔和 agent 生成时间。下述轮询 consumer 是参考实现。

## 2. 配置步骤（按顺序做）

### 2.1 建 Slack App（5 分钟）

1. 打开 https://api.slack.com/apps → **Create New App → From a manifest** → 选你的 workspace
2. 粘贴本目录 `manifest.yaml` 全文（先把 `display_name` 改成你的 bot 名）→ Create
3. **拿两个 token**：
   - `OAuth & Permissions` 页面顶部 → **Bot User OAuth Token**（`xoxb-` 开头）
   - `Basic Information` → **App-Level Tokens** → Generate（勾 `connections:write`，`xapp-` 开头）
4. **开消息开关**：`Features → App Home → Messages Tab` → 勾选 *Allow users to send messages*（否则用户私信 bot 会看到"发送消息功能已关闭"）
5. 想让 bot 在频道里被 `@`：把 bot **邀请进频道**（`/invite @你的bot名`）

### 2.2 部署代码（10 分钟）

```bash
git clone https://github.com/Ares-X/Agents-Slack-Bridge.git /opt/Agents-Slack-Bridge
cd /opt/Agents-Slack-Bridge/muse

cp .env.example .env && chmod 600 .env
# 编辑 .env，填入 xoxb- / xapp-（以及代理配置，如果有）
# 多 agent 协作：填 ALLOWED_BOT_USERS / ALLOWED_BOT_IDS（对端 U…/B…）

python3 -m venv venv
./venv/bin/pip install slack_sdk
```

### 2.3 常驻服务（5 分钟）

```bash
# 编辑 slack-bridge.service，把 /path/to/ 换成实际路径后：
sudo cp slack-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slack-bridge.service

# 验证：
systemctl is-active slack-bridge.service   # → active
tail -f bridge.log                          # → "socket mode connected, listening"
```

> ⚠️ `/etc` 下的文件在容器/VM 重建后会消失。**正本永远留在本目录**，并配一个每分钟的健康检查（不存在则从仓库复制 + `daemon-reload` + `restart`）。

### 2.4 接消费层（二选一）

| 方案 | 说明 | 延迟 | 上下文 |
|---|---|---|---|
| **A. `consumer/poll_consumer.py`**（参考实现） | 轮询 inbox → 按 channel 维护 `channel_sessions.json` 会话 → 调你的 LLM → `send.py` 发回 | ~1 分钟 | 按 channel 隔离，文件持久化 |
| **B. 平台 side chat**（如 Muse） | 定时任务把消息转给各 channel 的独立子对话，子对话里的 agent 回复 | 1~3 分钟 | 子对话天然隔离 |

> ⚠️ 方案 A 的 `generate_reply()` **默认只是 echo 示例**（`收到：…`，mention 已脱敏），**不是真实回复**。生产使用必须换成你的模型调用：把函数体替换为 LLM 请求，返回 `str`（正文）或 `(str, [uid...])`（正文 + 显式点名，经 `send.py --mention` 发出真实 @）。
>
> 真实回复位置：跟随上下文——在 thread 里被 @ 就在同一 thread 回（`send_durable.py --thread-ts <ts>` / `send.py --thread-ts`），新起话题才发顶层。不要一律改顶层，会破坏 thread 上下文。
>
> 已验证范围：`tests/` 隔离行为测试（并发入队、跨频道同 ts、ack 语义、发送状态机、mention 脱敏、子类型过滤，详见 §8）；**真实 Slack 联调（NOT_EXERCISED）**——未在授权测试频道执行，需部署者按 §6 自行验证。

用 A：把 `generate_reply()` 换成你家 agent 的调用（务必使用传入的 `history` 上下文），`nohup`/`systemd` 跑起来即可。脚本会从 `muse/` 根目录调用 `inbox_peek.py` / `send.py` / `channel_history.py`。多 agent 协作前先配好 bot 白名单；回复位置跟随上下文（thread 里被 @ 就跟帖）。

### 2.5 真实消费入口：`send_durable.py`（hook → agent 链路）

轮询 consumer（方案 A）是参考实现。生产真实链路是：外部 hook 轮询 `inbox.jsonl` → 唤醒 agent → agent 用真实模型生成回复正文 → 经 `send_durable.py` 单次投递：

```bash
printf '%s' "$REPLY" |
  python send_durable.py '<channel>:<ts>' [--thread-ts '<thread_ts>'] [--mention '<UID>']
```

- 正文经 stdin 输入，不含 echo，不替换 agent 的模型逻辑。
- 与 `poll_consumer` 共用同一套持久化状态机（`deliver_one`）：发送前 fsync 持久 claim、tombstone 防重复、`client_msg_id` 关联、限流通 `retry_wait` 持久化、`uncertain` fail-closed、已发送但 ACK 失败转 `unacked`（只重试 ACK，绝不重发正文）。
- `--thread-ts`：在 thread 里被 @ 就传同一 thread 回复；新起话题不传（顶层）。
- 恢复核验以发送尝试持久化的 `channel`/`thread_ts` 为权威，后续调用不得改变核验目标；线程核验经 `conversations.replies` 分页读取。
- 每次判断前重新读当前频道原文；来源在 thread 中时还要读完整 thread。摘要与入队时的旧文本只能作为定位线索。审批卡可能随批准而编辑，先根据 bot 身份、结构和最新状态理解控制语义；不要仅因旧 `Command approval` 与新 `Approved once` 不同就称为冒充，更不要把卡片命令当作新的执行授权。引用其他 agent 的动作时明确归属；没有自己的发送/执行证据时不要把他人的动作认领为自己执行，也不要作无证据的绝对否认。
- 如果本消息不需要实质回复，使用 `python send_durable.py '<channel>:<ts>' --no-reply --reason '具体原因'`，无需 stdin，且不能混用发送选项。该路径不查询 bot 身份、不调用 Slack；在发送状态锁内确认目标没有任何 `sending` / `uncertain` / `unacked` / `retry_wait` 后，写入带 `disposition=no-reply`、原因和来源证据的 inbox tombstone，并确认文件和目录 fsync。到期的 `retry_wait` 也不得直接静默结束；有发送状态时按原恢复流程处理。
- 静默完成重复调用保留原原因和证据，重新确认持久化；未知来源、存储失败或损坏不返回成功。compact 在既有七天去重保留期内保留完整 tombstone，包括原因与来源证据。不要用 raw `inbox_ack.py` 代替静默决定。
- 同一任务需要持续推进时继续接新消息；同一来源最多一条实质回复。真实交接（包括开放讨论和游戏）经显式 `--mention <UID>` 触发下一位，正文的纯文本名字本身不会唤醒对方。
- 将接收人判断放在实际 hook 的发送步骤开头：本条要求谁回答、审查、合并、修改、决定或反馈，就向谁传一次 `--mention`。对方已被根任务提及或正在讨论，不代表会收到这次交接。只分享信息或最终结果且没有下一步请求时不提及；不要机械地回提原作者。积压消息先按最新任务状态合并判断，已被后续版本处理的旧请求按原因静默完成。
- `uncertain` 核验缺少关联证据时，agent 不得自行调用 `inbox_store.ack()` / `SendState.resolve()` 或修改状态文件来宣称成功。保留状态与已有证据，在维护入口报告；人工审查是上报边界，不是 agent 自行跳过核验的许可。不要重发正文。

退出码：

| 码 | 含义 | 调用方动作 |
|---|---|---|
| 0 | 已发送+已确认、静默完成，或已被其他消费者完成/幂等跳过 | 本来源已完成 |
| 75 | 限流，`retry_at` 已持久保存 | 到期前不得重试 |
| 1 | 明确未发送（claim 已释放） | 可重试 |
| 2 | 结果不确定，保持 `uncertain` | 不得盲目重发，先查频道历史核验 |
| 3 | 已发送但 ACK 失败（`unacked`） | 只重试 ACK，绝不重发正文 |
| 4 | 发送状态损坏、存储失败或静默来源不可确认 | fail-closed，保留证据并处理原因 |

只有完成标记确实持久化才返回 0；发送后连续 ACK 失败保持 3，直到真正确认。静默遇到现存发送状态时按上表的 2/3/75 保持原状态，不静默丢弃。

## 3. 文件清单与路径

```
Agents-Slack-Bridge/
└── muse/
    ├── README.md                 # 本文档
    ├── manifest.yaml             # Slack App 定义（从 manifest 建应用）
    ├── .env.example              # 凭据模板 → 复制为 .env（0600，不提交）
    ├── bridge.py                 # ★ 核心：Socket Mode 监听 → inbox.jsonl（只收不发；先落盘后 ACK）
    ├── inbox_store.py            # 队列存储：append-only + tombstone ack，锁文件并发控制
    ├── send.py                   # 发消息：echo "正文" | python send.py <channel> [--thread-ts <ts>] [--mention <UID>]...
    ├── inbox_peek.py             # 打印未处理消息（不标记）
    ├── inbox_ack.py              # 按 <channel:ts> 标记已处理（处理成功后调）
    ├── channel_history.py        # 拉频道最近 N 条（python channel_history.py <channel> [N]）
    ├── resolve.py                # ID → 显示名（user|channel）
    ├── net_config.py             # 代理/CA 统一读取（见 §4c）
    ├── send_durable.py           # ★ 真实消费入口：hook→side chat→agent 的单次持久发送（见 §2.5）
    ├── slack-bridge.service      # systemd unit（改路径后用，WorkingDirectory=.../muse）
    ├── consumer/
    │   └── poll_consumer.py      # 消费层方案 A 参考实现（~30s 轮询；默认 echo 示例，需接模型）
    └── tests/
        ├── test_store.py         # 并发入队/跨频道同 ts/ack 语义/compact
        ├── test_bridge.py        # 事件分类/子类型过滤/bot 白名单
        └── test_consumer.py      # mention 脱敏/发送状态机/历史降级
```

运行时产生（不提交）：`inbox.jsonl`（队列）、`bridge.log`（日志）、`consumer/channel_sessions.json`（会话）。

所有脚本以 **`muse/` 为 cwd** 运行（`WorkingDirectory=.../muse`）。

## 4. 消息队列格式

`inbox.jsonl` append-only，每行一条。消息身份统一为 `msg_id = "<channel>:<ts>"`
（入队去重与确认都用它，跨频道同 `ts` 互不干扰）：

```json
{"msg_id":"C...:123.456","channel":"C...","user":"U...","text":"@bot 你好",
 "kind":"mention","ts":"123.456","thread_ts":"","received_at":1234567890.0}
{"type":"ack","msg_id":"C...:123.456","at":1234567900.0}
```
- `kind`: `dm`（私信）/`mention`（被@）。热路径不做名称查询，显示名用 `resolve.py` 按需解析。
- 确认是追加 tombstone（`{"type":"ack",...}`），**不做原地重写**——中途中断不会损坏已存消息。
- 消费流程：`inbox_peek.py` 读未确认消息 → 处理 → `inbox_ack.py <channel:ts>` 确认（失败不确认，下轮重试；退出码非 0 = 未确认，调用方不得重发正文）。
- 维护：`python -c "import inbox_store; print(inbox_store.compact())"` 清理已确认记录。
- 旧队列升级：pre-`msg_id` 时代的队列（无 `msg_id`、用 `delivered` 标记）首次被操作时自动迁移为 v2：`delivered=true` 转成 ack tombstone（**绝不**重播已处理消息），`delivered=false` 补上计算出的 `msg_id`。迁移是 temp+fsync+replace 原子重写，可重复跑、可崩溃恢复。

## 4b. 可靠性语义（防重复发送 / 队列 / 恢复）

发送端（`consumer/poll_consumer.py` + `send_state.py`）：
- **发送前先持久化领取**：`spawn send.py` 之前把 `sending` 状态 fsync 落盘。进程崩溃后重启，`sending` 一律转为 `uncertain`，**永远不会**恢复成"可直接发送"。
- **领取时检查持久完成标记**：`claim()` 在同一临界区（`send_state` EX → `inbox` SH）内检查 inbox 的 ack tombstone；另一个 consumer 已经发送+ack+resolve（删条目）后，拿旧快照的 consumer 再领取会得到 `completed`，直接跳过、绝不发送。已配置的队列缺失或读取失败时拒绝领取，不能把读取失败当作“没有完成记录”。
- **只有"明确证明未发送"才重试**：`send.py` 用 `RESULT not-sent` / `sent ok: False` 报告死在 API 调用之前或被 API 明确拒绝；超时、连接中断（`RESULT uncertain`）、输出含糊一律视为不确定，走 history 核验，绝不自动重发。
- **限流持久延期**：`send.py` 收到 HTTP 429 / `ratelimited` / `rate_limited` 后只返回 JSON `{"result":"retry_wait","retry_at":截止时间}`，退出码为 75，本进程不重发。consumer 将该期限持久保存为 `retry_wait`；未到期不发送、不核验、不 ACK，到期后才原子领取新的发送尝试。`Retry-After` 不得因单次进程超时预算被截短；缺失或无效时默认等待 60 秒。重启保留期限，并发只有一个消费者能重新领取，迟到的旧尝试核验不能覆盖延期或新尝试。其他调用 `send.py` 的平台消费层也必须处理退出码 75、保存 `retry_at`，到期前不得重试。连接断开和其他未知结果仍走 uncertain；SDK 自动重试保持关闭。
- **核验必须五项全中**：是我们自己的 bot（`SLACK_BOT_ID`/`SLACK_BOT_USER_ID` 命中其一）、频道/线程一致、消息时间不早于本次发送尝试（允许 60s 时钟偏差）、全文精确匹配（sha256）、**携带与本次发送尝试相同的 `client_msg_id`**。`client_msg_id` 是每次 claim 生成并随 POST 提交的唯一 id，只有 history 实际回显时才能作本实现的关联证据；不能假定 Slack 每次都会回显。同身份+同正文+时间窗口不能单独证明成功（30 秒前发过相同正文也会命中）。证明不了就保持 uncertain，3 轮无结论转人工日志；旧版本条目或 history 缺少 `client_msg_id` 时都不能自动确认，更不能由 agent 手工清状态替代核验。
- **ack 抛异常按确认失败处理**：发送成功但 `inbox_ack.py` 子进程崩溃/超时抛异常时，条目记为 `unacked`（只重试 ack），绝不卡在 `sending`。
- **状态文件损坏不静默**：`send_state.json` 加载时按 primary → `.tmp` → `.bak` 顺序尝试，每个候选都要过严格校验（版本号 == 2、`sends` 为 dict、每条 status 合法；未知版本号绝不"迁移"成空状态）。`.tmp` 是崩溃写一半留下的 fsync 过的**更新**版本，恢复安全；`.bak` 是上一次保存**之前**的旧版本——如果 primary 和 `.tmp` 都不可用而只有 `.bak` 可解析，恢复可能是 stale 的（会丢失已持久化的领取记录、把已发送的消息变回可发送），此时**拒绝恢复**：隔离证据并抛 `StateCorruptError`，consumer 在此期间**拒绝发送**（fail-closed），直到人工恢复经证明安全的副本。全部不可用时隔离为 `send_state.json.corrupt.<毫秒>.<primary|tmp|bak>`（每个来源独立后缀、证据不互相覆盖）并留 `.quarantined` 标记，重启也不许静默变空。

队列端（`inbox_store.py`）：
- **去重 tombstone 保留期限：7 天**（`TOMBSTONE_RETENTION_SECONDS`）。Slack 的 at-least-once 重投发生在分钟级，7 天是慷慨上界。
- **恢复规则**：(1) 保留期内重投 → tombstone 命中 → 不再入队；(2) tombstone 过期并被 compact 清掉后重投 → 视为新消息重新入队（at-least-once 的显式取舍；需要更长可调大保留期）；(3) 崩溃导致的队尾残行在下次写入前被隔离为 `inbox.jsonl.corrupt.*`（保留证据）再截断，绝不把新记录粘到残行上。
- **每次成功入队都确认持久化**：新消息与重复消息返回前都确认文件及目录 fsync。首次创建或 compact 后目录同步失败，即使文件已可见，后续不同消息也不能跳过确认；同步失败则抛异常，调用方不得向 Slack ACK（等重投）。
- **目录持久化在成功边界确认**：迁移恢复（replace 后）、首次创建文件、重复追加、ack 首次创建文件，成功返回前都要确认目录 fsync；目录同步持续失败时持续抛异常、持续拒绝 ACK，绝不确认一次可能因崩溃丢失的消息。

## 4c. 出站代理 / CA 配置（issue #7）

`bridge.py` / `send.py` / `channel_history.py` / `resolve.py` / `resolve_bot_identity()` 经 `net_config.read_proxy_config()` **统一读取**，顺序为：

1. `.env` 的 `PROXY_URL` / `CA_BUNDLE`（部署本地值）
2. 标准环境变量 `https_proxy` / `HTTPS_PROXY`、`SSL_CERT_FILE`
3. 都没有 → 直连（`None`），TLS 验证保持开启（绝不禁用）

如实说明：

- 在 cron / worker 等 stripped env（无代理变量）下真正起作用的是**部署 `.env` 里持久化的 `PROXY_URL`**；`net_config` 本身在两者都空时仍返回直连，**不声称**自动发现不存在的配置。
- 仓库通用版不硬编码任何内网代理地址或秘密；`.env.example` 只放占位符，`.env` 永不提交。
- 五个调用方行为一致：同一份 `.env` + 同一进程环境 → 同一代理决策。

## 5. 踩坑清单（实测）

1. **"向此应用发送消息的功能已关闭"** → manifest 漏了 `messages_tab_enabled: true`，去 App Home 手动开。
2. **`/etc` 文件消失** → 见 §2.3 警告，正本放本目录 + 健康检查自愈。
3. **出站代理/TLS 拦截** → `.env` 配 `PROXY_URL` / `CA_BUNDLE`，三个脚本都会读。
4. **频道必须先邀请 bot**，否则收不到 `app_mention`。
5. **thread 回复唤醒**：人类用户在 bot 消息的 thread 下回复也会入队（kind=`thread_reply`，bot 的 thread 回复不入队以防回环）。这需要 App 的 Event Subscriptions 里订阅 `message.channels`（见 `manifest.yaml`）；订阅缺失时 thread 回复事件根本发不到桥上。处理顺序是**先查父消息、再落盘、最后 ACK**：用 `conversations_replies(limit=1)` 确认父消息是自己发的才入队；查询失败/父消息不明/落盘失败一律不 ACK，靠 Slack 重发重试，绝不当成非目标消息丢弃。
5. **多 agent 协作（默认开）**：其他 bot 的 @mention 默认放行，自己的消息永远过滤（防自循环）。要收紧成白名单，把协作对象的 user ID / bot ID 填进 `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS`（任一非空即白名单模式）。防回环纪律：每条来源至多一条实质回复；控制/ACK 消息按上下文静默完成；任务可持续多轮，真实交接显式 mention，转述名字用纯文本。
6. **回复前读上下文**：消费层应先 `channel_history.py` 再生成回复（参考 `poll_consumer.py`）。
7. **可靠性顺序：先落盘，再 ACK**：`bridge.py` 收到事件后先 `flush+fsync` 写入队列，**成功后才**向 Slack 发 ACK；名称查询等慢操作不在热路径。落盘失败则不 ACK，靠 Slack 重发 + `msg_id` 去重实现 at-least-once（重复入队会被去重丢弃）。
8. **mention 回声脱敏**：默认 echo/转述必须把 `<@U...>` 转成纯文本 `@U...`（不触发通知）；主动点名走 `send.py --mention <UID>` 显式发出。不要为了防回环禁掉全部 mention。
9. **子类型白名单**：DM 里只有无 subtype 和 `me_message` 被当作新消息；`message_changed` / `message_deleted` 等只 ACK 不入队。
8. **中断期消息会丢**（Slack 不补发），健康检查把中断窗口压到分钟级。
9. **回复位置**：在 thread 里被 @ 就在同一 thread 回（传 `--thread-ts`）；新起话题才发顶层。不要一律改顶层，会破坏 thread 上下文。

## 6. 最小验证

1. `systemctl is-active` → active，日志出现 `socket mode connected`
2. Slack 私信 bot → `python inbox_peek.py` 看到这条 → `echo hi | python send.py <DM频道ID>` → Slack 收到
3. 拉 bot 进测试频道，`@bot hello` → 收到 mention → 消费层回复出现在频道

## 8. 隔离行为测试

```bash
cd muse && python3 -m unittest discover -s tests -v
```
测试覆盖：并发入队不丢不重、跨频道同 `ts`、tombstone ack 语义、compact、事件子类型过滤、bot 白名单两种模式、mention 脱敏/显式点名、ack 失败不重发、发送结果不确定不盲目重发、历史失败延迟→降级。`test_reliability.py` 另有行为测试：旧队列升级（含 delivered=false→true 顺序无关的完成标记优先）、发送结果判定、发送后崩溃恢复、并发 claim 互斥、存储失败（fsync/损坏隔离）、错误回执匹配、wire text 哈希（含 mention 追加）、队尾截断、compact 后重投。`test_reliability_round2.py` 覆盖 stale `.bak` 恢复拒绝、严格版本/结构校验、分来源隔离证据、ack 异常、旧快照、`client_msg_id` 核验和目录持久化。恢复测试另外覆盖目录持续故障下真实 bridge handler 不 ACK、完成记录读取失败不重发，以及限流 60/120 秒跨重启等待、到期领取互斥、错误期限拒绝和迟到核验。`test_natural_collaboration.py` 另覆盖频道/线程分页去重、完整正文和控制卡、分页失败不输出部分成功、静默原因/来源持久化及 compact 保留、并发领取互斥、所有未完成发送状态拒绝静默、fsync 故障与重复确认。仅标准库与模拟网络，无新增依赖。

**NOT_EXERCISED**：真实 Slack 联调未在授权测试频道执行（无凭据、无部署修改），需部署者按 §6 自行验证。

## 7. 安全

- token 只在 `.env`（0600），**永不**进聊天记录、日志、命令行参数、git
- 建议首次配置后去 Slack 后台轮换一次 token
- bot 只能读它加入的频道 + 自己的私信；不想让它看的地方别邀请它
