# Agents Slack Bridge

**Connect Muse, Grok Bot, Hermes, and ChatGPT DOTS for collaboration in Slack.**

**English** · [简体中文](./README.zh-CN.md)

[Agent-led setup](#get-started) · [Collaboration capabilities](#how-far-can-the-agents-collaborate) · [Choose an integration](#choose-an-integration) · [Validation](./docs/validation.en.md)

Use Slack as a shared workspace for AI agents. Start a task through a direct message or native mention, let agents from different platforms read the relevant context, ask questions, review and revise, and deliver a shared result.

This repository contains **4 integration guides across 3 architectures**: local bridge implementations, native Hermes configuration and patches, and a hosted DOTS connection guide. Each route uses its own model and runtime; choose the one you need.

**Here to configure an agent? Start with [AGENTS.md](./AGENTS.md).** The agent should identify its target runtime, inspect existing setup, ask once for missing prerequisites, then configure, load, and verify the integration. The [setup workflow](./docs/setup.en.md) spells out what the agent handles and what needs the user.

## How far can the agents collaborate?

**Demonstrated level: a single human brief can lead to a self-organized, multi-agent discussion, peer review, iterative revisions, and a shared final result.** With the integrations configured, the agents can continue the task by mentioning one another; the user does not need to relay every message or prescribe the speaking order.

| Workflow | What the agents can do | Evidence |
|---|---|---|
| Direct assistance | Answer a native channel mention, or a DM where supported, using relevant task context | Channel replies observed from all four agents; DM support depends on the route and account |
| Peer handoff | Ask another agent a question, request a review or next step, and incorporate its reply in the channel or task thread | Bidirectional handoffs tested across the four-agent deployment |
| Open group discussion | Propose alternatives, choose roles and a writer, challenge omissions, withdraw older proposals, and revise together | Four agents completed an open text-design task without manual prompting between turns or a scripted relay |
| Shared result and completion | Have one writer integrate accepted feedback, state unresolved issues, and finish without acknowledgement loops | Shared final version delivered; one redundant summary followed, with no continued loop |

The agents retain their own models, tools, sessions, and permissions. They build shared task context by reading the relevant Slack messages and threads. Planning and judgment happen in those agents; the integrations provide the message paths and delivery handling.

This can support workflows such as discussing a plan, reviewing a proposal, or asking a tool-enabled peer to implement and check a change. **Actual code edits, tests, deployments, and other actions depend on each agent's existing tools and user authorization.** End-to-end unattended software delivery has not been established by the collaboration test.

## Choose an integration

| Agent | Integration | Prerequisites | Start here |
|---|---|---|---|
| **Muse** | Local Socket Mode bridge → queue → hook / agent | Python environment, Slack App, real agent consumer | [Setup guide](./muse/README.md) |
| **Grok Bot** | Local Socket Mode bridge → queue → `agent_wake` | Python environment, Slack App, external agent webhook | [Setup guide](./grokbot/README.md) · [Agent checklist](./grokbot/AGENT.md) |
| **Hermes** | Hermes' native Slack gateway / platform adapter | Installed Hermes, Slack App, configuration for that version | [Setup guide](./hermes/README.md) · [Agent checklist](./hermes/AGENT.md) |
| **ChatGPT DOTS** | Hosted Slack connection and message event subscription | Slack connection and event capabilities available to the account | [Integration guide](./chatgpt-dots/README.md) · [Agent checklist](./chatgpt-dots/AGENT.md) |

Muse / Grok use outbound Socket Mode connections without a public inbound endpoint. Hermes uses its own gateway. The DOTS route depends on account capabilities and organization policy: it is a community guide, has no local installer, and its example configuration is not an official import format. The integration-specific guides linked above are currently in Chinese.

## Get started

Send the repository URL to the agent you want connected, with this instruction:

> Set up this repository for my agent. Read the root AGENTS.md, identify the target runtime, and inspect existing configuration. Ask me once for all missing prerequisites and human-only actions; never ask for secrets in chat. Then complete configuration, load it, and verify real responses and collaboration in the agreed Slack channel. Preserve existing work and report any blockers with evidence.

The agent follows [agent-led setup](./docs/setup.en.md): **discover → collect missing prerequisites → configure → load → verify**. It should handle IDs, configuration edits, real model/hook wiring, supervision, and tests wherever its authorized tools allow. The user supplies only missing choices, consent, secure credential entry, or unavailable account access.

This is a workflow for a capable agent, not a universal one-click installer. Muse still needs a real model consumer/hook; Grok needs an actual agent wake receiver; Hermes needs a working model and compatible gateway; DOTS needs account-supported events. Slack/admin authorization may require user interaction. If a prerequisite is absent, the agent must name it rather than report an echo as success. Fresh automatic setup across all platforms remains unverified.

For manual setup, use the integration table and the same [acceptance checklist](./docs/setup.en.md#4-verify-the-intended-behavior).

### Try a shared task

In a configured collaboration channel, select the participants using Slack's mention menu, then send:

> Design a three-minute icebreaker that uses only text in Slack. Divide the work yourselves, identify and fix problems in each other's rules, and choose one writer to submit the shared final version. State any unresolved disagreements. Design it without starting the game, and stop discussing when finished.

The agents choose their own speaking order and writer. Use a native mention when asking a peer to act; reports, quotations, and thanks do not need another mention. See the [collaboration rules](./docs/operations.en.md).

## Tested scope and limits

On **2026-10-04**, Muse, Grok Bot, Hermes, and DOTS designed a text-only Slack game together and reached a shared final version in **6 min 07 sec**. They chose the writer and corrected missing rules through peer feedback. A separate Muse task verified one send, matching receipts, and a durable ACK. These observations come from one configured deployment; the game itself was not run.

- **Human control remains in place.** The user defines the task and allowed scope. Peer messages do not grant new permissions, and actions that require approval still require it. The demonstrated autonomy is the conversation and revision process within that scope.
- **Timing and scale are bounded by the runtime.** Separate single-agent checks took 36 sec and 137 sec end to end. These are observations, not latency targets. The test covered four participants in one channel; arbitrary concurrency, larger groups, and long-running unattended work remain unverified.
- **Delivery has explicit uncertainty.** Local bridges retain queues and send state to prevent blind resends. Missing receipt evidence can leave a message awaiting resolution. Neither these tests nor the hosted routes establish a universal exactly-once guarantee.

Reproduce the acceptance checks in your own environment, including real agent wiring, current context, account capabilities, and installed versions. Detailed evidence and failure boundaries are in [Validation](./docs/validation.en.md).

## Documentation

| Document | Covers |
|---|---|
| [Agent entry point](./AGENTS.md) · [Setup workflow](./docs/setup.en.md) | Route selection, one prerequisite handoff, configuration, loading, and acceptance |
| [Muse](./muse/README.md) / [Grok Bot](./grokbot/README.md) | Local bridges, real agent wiring, delivery and recovery |
| [Hermes](./hermes/README.md) | Native gateway, authorization, mention plugin, and version-specific patches |
| [ChatGPT DOTS](./chatgpt-dots/README.md) | Hosted connections, event capabilities, and configuration scope |
| [Operations](./docs/operations.en.md) | Three architectures, reply routing, collaboration, deployment handoff, and troubleshooting |
| [Validation](./docs/validation.en.md) | Acceptance layers, live test scope, related fixes, and historical reviews |

## Contributing

Submit a PR for the relevant agent directory, explaining the problem, change, checks, and untested areas. Coordinate changes across directories and preserve existing configuration, queues, and sessions. Do not commit tokens, real queues, private instance identifiers, or chat logs. Preserve evidence when delivery is uncertain; clearing state is not proof of success. See [Contributing and handoff](./docs/operations.en.md#contributing-and-handoff).
