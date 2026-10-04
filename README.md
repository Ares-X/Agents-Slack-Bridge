# Agents Slack Bridge

**让 Muse、Grok Bot、Hermes 和 ChatGPT DOTS 在 Slack 中沟通与协作。**

**简体中文** · [English](./README.en.md)

[选择接入方式](#选择接入方式) · [开始使用](#开始使用) · [协作与运维](./docs/operations.md) · [验证记录](./docs/validation.md)

把 Slack 作为多个 AI agent 的共同工作空间：通过私信或原生提及发起任务，让不同平台的 agent 读取相关上下文、互相提问、审查和修订，最后交付共同结果。

本仓库提供 **4 种接入指南、3 类架构**，包括本地桥接代码、Hermes 原生接入配置与补丁，以及 DOTS 托管连接指南。各路线复用自己的模型与运行环境，按需选择。

## 可以做什么

- **在 Slack 中找 agent**：通过私信、频道提及和相关线程交互。
- **让 agent 共同完成任务**：支持必要的原生提及交接、最新上下文读取和多轮修订。
- **让对话在完成后停下来**：合并过时待办，对无新动作的确认消息静默处理，保留处理原因。
- **保留消息处理证据**：本地桥接路线提供持久队列、去重和发送状态管理；发送结果不明时保留状态，避免盲目重发。

## 选择接入方式

| Agent | 接入方式 | 需要准备 | 入口 |
|---|---|---|---|
| **Muse** | 本地 Socket Mode bridge → 队列 → hook / agent | Python 环境、Slack App、实际 agent 消费入口 | [部署指南](./muse/README.md) |
| **Grok Bot** | 本地 Socket Mode bridge → 队列 → `agent_wake` | Python 环境、Slack App、外部 agent webhook | [部署指南](./grokbot/README.md) · [Agent 配置清单](./grokbot/AGENT.md) |
| **Hermes** | Hermes 自带 Slack gateway / 平台适配器 | 已安装的 Hermes、Slack App、对应版本配置 | [部署指南](./hermes/README.md) · [Agent 配置清单](./hermes/AGENT.md) |
| **ChatGPT DOTS** | 托管 Slack 连接与消息事件订阅 | 当前账号可用的 Slack 连接及事件能力 | [接入指南](./chatgpt-dots/README.md) · [Agent 配置清单](./chatgpt-dots/AGENT.md) |

Muse / Grok 的 Socket Mode 使用出站连接，无需公网接收入口。Hermes 使用自己的 gateway。DOTS 路线依赖账号与组织策略，本目录提供社区指南，没有本地安装器；其配置示例也不是官方可导入格式。

## 开始使用

1. **选择上表中的路线。** 已有部署先检查当前配置；首次使用可克隆仓库：

   ```bash
   git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
   cd Agents-Slack-Bridge
   ```

2. **按对应指南接入一个 agent。** 确认工作区、频道、允许协作的对端，以及实际模型入口。凭据保留在私有配置中。

   Muse 的参考 poll consumer 默认是 echo；Grok 的 `agent_wake` 需要外部 agent 接线，模板仅在显式选择时启用。桥接收到消息与真实 agent 能够作答，需要分别验证。

3. **从单条消息验证到共同任务。** 先测试私信或提及，再确认线程路由、上下文和双向交接，最后尝试开放讨论。具体标准见[分层验收](./docs/validation.md#分层验收)。

### 试一次共同任务

在已配置的协作频道中，用 Slack 的提及菜单选中参与者，再发送：

> 一起设计一个三分钟、只用 Slack 文字就能玩的破冰游戏。请自行分工，互相指出规则中的问题并修订，选一位提交共同终稿；未解决的分歧请注明。只设计，不开局，完成后停止讨论。

发言顺序和执笔者由 agent 自行协商。需要对方行动时原生提及对方；纯报告、引用和感谢不继续点名。更多规则见[协作与运维](./docs/operations.md)。

## 验证情况

2026-10-04，一次已配置的四 agent 部署完成了开放任务测试：自主提案、交叉审查与修订，形成共同终稿，期间没有人工催答或固定接力。Muse 的后续独立测试确认了单次发送、回执关联与持久 ACK。

这些结果针对该次部署。响应延迟、托管账号能力、版本兼容和故障恢复仍需在自己的环境验证；不能据此承诺任意并发场景或 exactly-once。测试范围、历史记录和已知限制集中在[验证记录](./docs/validation.md)。

## 文档导航

| 文档 | 内容 |
|---|---|
| [Muse](./muse/README.md) / [Grok Bot](./grokbot/README.md) | 本地 bridge、真实 agent 接线、发送与恢复 |
| [Hermes](./hermes/README.md) | 原生 gateway、授权、提及插件与版本补丁 |
| [ChatGPT DOTS](./chatgpt-dots/README.md) | 托管连接、事件能力与配置范围 |
| [协作与运维](./docs/operations.md) | 三类架构、回复位置、协作规则、部署交接与排障 |
| [验证记录](./docs/validation.md) | 分层验收、实测范围、修复关联与历史审查 |

## 参与改进

围绕对应 agent 目录提交 PR，说明问题、变更、验证结果和未验证项。跨目录改动先协调，保留已有配置、队列和会话。不要提交 token、真实队列、私有实例标识或聊天记录；发送结果不明时保留证据，不通过清状态制造成功。详见[贡献与交付](./docs/operations.md#贡献与交付)。
