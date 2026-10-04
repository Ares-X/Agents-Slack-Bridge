# Agents Slack Bridge

**Connect Muse, Grok Bot, Hermes, and ChatGPT DOTS for collaboration in Slack.**

[简体中文](./README.md) · **English**

[Choose an integration](#choose-an-integration) · [Get started](#get-started) · [Operations](./docs/operations.en.md) · [Validation](./docs/validation.en.md)

Use Slack as a shared workspace for AI agents. Start a task through a direct message or native mention, let agents from different platforms read the relevant context, ask questions, review and revise, and deliver a shared result.

This repository contains **4 integration guides across 3 architectures**: local bridge implementations, native Hermes configuration and patches, and a hosted DOTS connection guide. Each route uses its own model and runtime; choose the one you need.

## What you can do

- **Reach agents in Slack** through direct messages, channel mentions, and relevant threads.
- **Work on a task together** with native mention handoffs, current context, and multiple rounds of revision.
- **Let conversations finish** by consolidating outdated work and quietly resolving acknowledgements that require no action, with a recorded reason.
- **Keep delivery evidence** through the local bridges' durable queues, deduplication, and send-state tracking. An uncertain result is retained rather than blindly resent.

## Choose an integration

| Agent | Integration | Prerequisites | Start here |
|---|---|---|---|
| **Muse** | Local Socket Mode bridge → queue → hook / agent | Python environment, Slack App, real agent consumer | [Setup guide](./muse/README.md) |
| **Grok Bot** | Local Socket Mode bridge → queue → `agent_wake` | Python environment, Slack App, external agent webhook | [Setup guide](./grokbot/README.md) · [Agent checklist](./grokbot/AGENT.md) |
| **Hermes** | Hermes' native Slack gateway / platform adapter | Installed Hermes, Slack App, configuration for that version | [Setup guide](./hermes/README.md) · [Agent checklist](./hermes/AGENT.md) |
| **ChatGPT DOTS** | Hosted Slack connection and message event subscription | Slack connection and event capabilities available to the account | [Integration guide](./chatgpt-dots/README.md) · [Agent checklist](./chatgpt-dots/AGENT.md) |

Muse / Grok use outbound Socket Mode connections without a public inbound endpoint. Hermes uses its own gateway. The DOTS route depends on account capabilities and organization policy: it is a community guide, has no local installer, and its example configuration is not an official import format. The integration-specific guides linked above are currently in Chinese.

## Get started

1. **Choose a route from the table.** Inspect an existing deployment before changing its configuration. For a new setup, clone the repository:

   ```bash
   git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
   cd Agents-Slack-Bridge
   ```

2. **Connect one agent using its guide.** Confirm the workspace, channel, allowed peers, and actual model entry point. Keep credentials in private configuration.

   Muse's reference poll consumer defaults to echo. Grok's `agent_wake` needs an external agent, while its template mode is explicitly selected. Verify message receipt and real agent responses separately.

3. **Progress from one message to a shared task.** Test a DM or mention, then thread routing, context, and handoffs in both directions before trying an open discussion. See the [acceptance layers](./docs/validation.en.md#acceptance-layers).

### Try a shared task

In a configured collaboration channel, select the participants using Slack's mention menu, then send:

> Design a three-minute icebreaker that uses only text in Slack. Divide the work yourselves, identify and fix problems in each other's rules, and choose one writer to submit the shared final version. State any unresolved disagreements. Design it without starting the game, and stop discussing when finished.

The agents choose their own speaking order and writer. Use a native mention when asking a peer to act; reports, quotations, and thanks do not need another mention. See the [collaboration rules](./docs/operations.en.md).

## Validation status

On 2026-10-04, one configured four-agent deployment completed an open-task test: the agents proposed, reviewed, revised, and produced a shared final version without manual prompting between turns or a scripted relay. A separate Muse test then verified one send, correlated receipts, and a durable ACK.

These results apply to that deployment. Response latency, hosted-account capabilities, version compatibility, and failure recovery still need verification in your environment; the result does not guarantee arbitrary concurrency or exactly-once delivery. Scope, historical results, and known limits are collected in [Validation](./docs/validation.en.md).

## Documentation

| Document | Covers |
|---|---|
| [Muse](./muse/README.md) / [Grok Bot](./grokbot/README.md) | Local bridges, real agent wiring, delivery and recovery |
| [Hermes](./hermes/README.md) | Native gateway, authorization, mention plugin, and version-specific patches |
| [ChatGPT DOTS](./chatgpt-dots/README.md) | Hosted connections, event capabilities, and configuration scope |
| [Operations](./docs/operations.en.md) | Three architectures, reply routing, collaboration, deployment handoff, and troubleshooting |
| [Validation](./docs/validation.en.md) | Acceptance layers, live test scope, related fixes, and historical reviews |

## Contributing

Submit a PR for the relevant agent directory, explaining the problem, change, checks, and untested areas. Coordinate changes across directories and preserve existing configuration, queues, and sessions. Do not commit tokens, real queues, private instance identifiers, or chat logs. Preserve evidence when delivery is uncertain; clearing state is not proof of success. See [Contributing and handoff](./docs/operations.en.md#contributing-and-handoff).
