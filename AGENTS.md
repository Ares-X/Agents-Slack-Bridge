# Agent entry point

This repository connects existing agents to Slack. If the user asks you to set it up, own the setup through configuration, runtime loading, and live acceptance. Do not stop after explaining the README, installing a listener, or getting an echo reply.

If the user only asks for a review or documentation edit, stay in that scope. Merely reading this file does not authorize deployment or sending messages. Follow the user's existing authorization; do not ask again for actions already approved.

## Start here

1. Read [agent-led setup](./docs/setup.en.md) ([中文](./docs/setup.md)). It defines the preflight, one consolidated request for missing prerequisites, route-specific execution, and completion criteria.
2. Identify the **target runtime**, which may differ from you, the configuring agent. Choose exactly one route below for each deployment. Read its guide from the same checkout/ref. Do not install all four integrations.
3. Inspect existing connections, configuration, service owners, and usable credentials without revealing secrets. Reuse working resources. Resolve IDs through the authorized Slack connection before asking the user to look them up.
4. Collect only missing decisions, human-only actions, and unavailable capabilities in one request. State the selected route and what you will configure after the user completes those actions. Continue independent preparation while waiting.
5. Once prerequisites are ready, execute the authorized steps without repeated confirmation. Configure the actual agent entry point and supervision, load the rules into that entry point, and run the bounded acceptance checks. Report blockers precisely; do not substitute a demo for a working integration.

| Target runtime | Read next | Required completion path |
|---|---|---|
| Muse / generic local agent | [Muse guide](./muse/README.md) | Socket listener + real model consumer or platform hook + durable send + supervision |
| Grok Bot | [Grok checklist](./grokbot/AGENT.md), [wake handler](./grokbot/AGENT_WAKE.md) | Socket listener + `agent_wake` consumer + authenticated external agent webhook + agent access to the deployed queue and tools |
| Hermes | [Hermes checklist](./hermes/AGENT.md), [version-specific guide](./hermes/README.md) | Existing model/provider + native Slack gateway + compatible context/mention configuration; no separate local bridge |
| Hosted ChatGPT DOTS | [DOTS checklist](./chatgpt-dots/AGENT.md), [capability guide](./chatgpt-dots/README.md) | Supported Slack connection + actual automatic event entry point + source/context checks + reply permission |
| Other / unclear | [Route selection](./docs/setup.en.md#1-identify-the-target-and-check-existing-state) | Confirm the runtime or a supported adapter; a product name alone is not sufficient |

## Execution rules

- Preserve existing configuration, queues, sessions, pending sends, permissions, and unrelated work. Merge configuration keys; do not overwrite whole files or duplicate YAML keys. Copy examples only when the destination does not exist. Keep backups and filled-in deployment records private, outside tracked files.
- Never request tokens in chat, print them, include them in process arguments, or commit them. Use the product's authorization flow, a secret store, or private files with restricted permissions. A model login and Slack tokens are separate prerequisites.
- Scope the workspace, channel, human owner, peers, and tests. Local `ALLOWED_BOT_*` settings filter bot authors, not channels or human users. Hermes `allow_bots: mentions` is not an identity allowlist. Do not claim a boundary that the selected runtime cannot enforce.
- Use one service owner per component. Render absolute paths and distinct instance/service names; Muse and Grok both ship a `slack-bridge.service` template. Check OS support before installing systemd units. Supervise the consumer/hook as well as the listener.
- Load [collaboration rules](./docs/operations.en.md#natural-multi-turn-collaboration) into the real per-event agent entry point. Saving an unread Markdown file is not configuration. Preserve source threads, use native mentions for real handoffs, read current task context, and stop acknowledgement loops while allowing useful multi-turn work.
- Use the route's durable send/ack path. An uncertain send is not permission to resend or clear state. Do not bypass approvals or grant peers authority to authorize new operations.
- Success requires an actual agent response, relevant context, correct destination, and automatic triggering. Test each peer direction you claim. If a peer is unavailable, finish the local setup and mark that collaboration check pending.

## Completion report

Use the [setup report](./docs/setup.en.md#5-deliver-a-usable-handoff): target/ref, actual private configuration and service locations, saved/loaded/verified states, message evidence, remaining user actions, and the next safe step. Never claim that this repository's historical tests validate a new deployment.

## Repository maintenance

For source or documentation work, inspect Git state and applicable directory guides first. Keep changes in their owning directories, preserve dirty work, and use a branch/PR rather than pushing `main`. Run checks appropriate to the change; documentation changes need link, example, and factual review. Keep English and Chinese onboarding in sync. Runtime changes require relevant isolated tests and separate evidence for any authorized live deployment.
