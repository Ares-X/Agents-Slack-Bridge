# Set up through an agent

[Project home](../README.md) · **English** · [简体中文](./setup.md) · [Agent entry point](../AGENTS.md)

Give your agent the repository URL and ask it to configure your Slack connection. The agent should discover what already exists, gather missing prerequisites once, complete the supported configuration, and verify the result. You should not need to translate each README step into a new instruction.

This is an **agent-run workflow**, not a universal unattended installer. Slack authorization, administrator approval, unavailable platform features, and model credentials can require the user. After those prerequisites are supplied, the agent should continue through acceptance in the same setup session. The four-agent collaboration has been tested; fresh setup from a bare URL on every platform has not.

## Give this to your agent

```text
Set up https://github.com/Ares-X/Agents-Slack-Bridge for my agent.
Read the root AGENTS.md and follow docs/setup.en.md.
Identify the target runtime and reuse existing connections and configuration.
First inspect what you can, then ask me once for all missing information,
permissions, or actions that only I can complete. Do not ask for secrets in chat.
Once those prerequisites are ready, complete configuration, load it, and run
bounded Slack acceptance tests in the agreed channel with the agreed peers.
Preserve existing work and permissions. Report what is configured, loaded,
verified, or blocked, with evidence; do not stop at an echo or a plan.
```

If the target, workspace, channel, or peers are already known from the conversation, use them. A repository URL alone does not tell an agent which account or deployment to change. An unknown required choice belongs in the single prerequisite request below.

## 1. Identify the target and check existing state

The configuring agent and the target agent can be different: Codex configuring a Hermes server must choose **Hermes**, not infer a new route from its own name.

| Target | Select this route when | Read before making changes |
|---|---|---|
| Muse / generic local agent | You can run Python and invoke a real model or platform hook that can consume the queue | [Muse](../muse/README.md) |
| Grok Bot | A real Grok agent can receive an authenticated wake webhook and access the deployed queue/tools | [Grok checklist](../grokbot/AGENT.md), [wake contract](../grokbot/AGENT_WAKE.md) |
| Hermes | The target runs Hermes with a usable model/provider | [Hermes checklist](../hermes/AGENT.md), [version requirements](../hermes/README.md) |
| Hosted ChatGPT DOTS | The account exposes a Slack connection and a supported automatic message-event route | [DOTS checklist](../chatgpt-dots/AGENT.md) |
| Other | None of these contracts matches the available runtime | Check whether the Muse generic consumer fits; otherwise report the missing adapter before claiming automatic setup |

Inspect, without starting another listener or consuming pending work:

- Target host/account, OS, installed runtime and version, model authentication, tools, and deployment permissions. Local bridges use POSIX facilities; do not apply the Linux/systemd instructions to native Windows or assume systemd exists in a container.
- Existing checkout/ref, dirty files, deployment paths, private configuration, queues, pending send state, services, hooks, and hosted subscriptions. Record which component owns each stage and how this change can be reverted without rolling back message state.
- Existing Slack app/connection, workspace and channel IDs, public/private channel type, human owner ID, own bot user ID, peer user IDs, channel membership, scopes, and events. Discover IDs through authorized tools; names are only labels.
- Network/proxy/CA needs under the actual service account; writable persistent state; ability to wake the real model and publish its reply. Check secret presence/access without dumping values.

For a fresh local checkout only:

```bash
git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
cd Agents-Slack-Bridge
git rev-parse HEAD
```

For an existing checkout, inspect it and record the deployed ref instead of cloning over it or resetting it. Read guides from that ref. A hosted agent may read the repository through its tools without a local clone.

## 2. Ask once for missing prerequisites

Fill the **known** values yourself. Send one concise checklist containing only missing items, why they are needed, and the exact user action. Do not ask the user to do work your authorized tools can already perform.

| Item | Agent should discover or prepare | User action only if needed |
|---|---|---|
| Target and persistence | Existing runtime, version, install directory, service owner, model access | Choose an ambiguous target; authorize required installation or service changes; complete provider login |
| Collaboration scope | Workspace/channel/peer IDs and current access | Choose the channel and peers; confirm any scope not already authorized, including bounded test messages |
| Slack app / connection | Reuse a suitable app; prepare the selected manifest or connection flow | Approve app install/reinstall or OAuth in Slack; obtain administrator approval if required |
| Private credentials | Detect usable credentials and name their private destination | Store missing tokens or webhook credentials in that destination or secret UI; return only “saved” |
| Channel access | Verify membership and ability to read/reply | Invite the bot or approve access if the agent cannot do so |
| Real agent entry point | Discover the supported model/hook/webhook/subscription and its configuration surface | Enable an unavailable account feature or provide access to the actual runtime |

Example request structure; replace bracketed text before sending:

```text
I found [target/version], and will use [route]. Already ready: [known items].
I still need:
1. [Missing choice or human-only action, exact location, reason.]
2. [Private credential destination / authorization flow; do not paste the value.]
After these are ready, I will configure [components], load them, and test
[agreed channel, peers and test scope]. Existing [connections/state] will be reused.
```

Reuse prior authorization. Do not ask again merely because a route guide says “after confirmation.” Wait for required answers, continue independent preparation, and resume after the user completes the checklist. If an unexpected blocker appears later, ask only for that new item and explain what changed; “ask once” is not a promise to bypass approvals or invent unavailable capabilities.

### Slack app handoff for local bridges and Hermes

Prepare the selected route's manifest, then use [Slack app settings](https://api.slack.com/apps) to create or update the authorized app. Install it to the intended workspace, enable Socket Mode, and obtain the **bot token** (`xoxb-`) plus an **app-level token** (`xapp-`, `connections:write`). Enable App Home messages for DM use and invite the bot to the test channel. Use private files/secret UI for the values. See Slack's [manifest guide](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/) and [Socket Mode setup](https://docs.slack.dev/apis/events-api/using-socket-mode/).

Compare an existing app's settings before changing them. Apply scope/event changes and complete any Slack-required reinstall/consent. An edited local manifest alone changes nothing in Slack. Muse's shipped manifest covers public channels and DMs; private-channel events/history need a separately checked scope/event change. Grok's manifest includes private-channel scopes/events. Hermes generates its manifest from the installed runtime. **Hosted DOTS uses its supported connection flow; do not create tokens or install these bridges for it.**

## 3. Complete the selected route

Run the selected directory guide with the corrections and completion requirements below. Merge private configuration, preserve existing values, and keep new credentials out of tracked examples. Local bridge `.env` loaders expect simple, unquoted `KEY=value` lines, not shell `export` syntax; use the route's actual loader. Reuse installed dependencies; where installation is needed, include it in the agreed setup scope. All local commands use the chosen virtual environment and the agent directory as the working directory.

### Muse / generic local agent

1. Follow [Muse setup](../muse/README.md): create `.env` only if missing; configure Slack tokens, verified identities/peers, proxy and CA when required. Set up Python and `slack_sdk` in the selected environment. Do not disable TLS verification.
2. Select **one actual consumer**. For a Muse platform hook, bind the real hook/scheduler to the deployed queue and the intended agent/side chat. It must read current channel/thread context, generate a real answer or quiet decision, and use `send_durable.py`. The platform hook and its registration API are not supplied by this repository; discover the supported host facility. If none is available, report the missing consumer capability.
3. For a generic local model, wire `consumer/poll_consumer.py`'s `generate_reply()` to the authorized model using its `history` and session arguments, then supervise that consumer. The shipped implementation is echo. Do not start both the reference consumer and a separate hook for the same workload.
4. Persist the collaboration instructions in the actual hook/model entry point. Use source `channel:ts` and `thread_ts`, explicit peer mentions, and the durable send/quiet/recovery contract in the Muse guide. Existing uncertain sends stay preserved.
5. Load and supervise the listener **and** the chosen consumer/hook. A connected socket without an automatically triggered model reply is incomplete.

### Grok Bot

1. Follow the [Grok checklist](../grokbot/AGENT.md): reuse/create private `.env`, install the declared `requirements.txt` in the selected environment, and configure verified Slack identities, peers, routing, proxy and CA.
2. Bind a real agent wake endpoint. Private `grokbot/webhook.env` needs `WEBHOOK_URL` and `WEBHOOK_KEY`; this is an authenticated **agent wake endpoint**, not a Slack Incoming Webhook. Keep it mode `0600`. Set `REPLY_MODE=agent_wake` explicitly in `.env` for clarity.
3. Configure the receiving agent to execute [AGENT_WAKE.md](../grokbot/AGENT_WAKE.md) and [peer rules](../grokbot/PEER_STANDING_RULES.md) against the **same deployment's** queue and tools. Set `GROK_BRIDGE_DIR` in its execution environment to the verified absolute deployment directory. A remote receiver needs authorized access to that host; a path in a webhook payload does not grant file access. Replies and quiet decisions use `pending_consume_once.py` / `reply_pipeline`, not raw send followed by ACK.
4. From `grokbot/`, run `./venv/bin/python wake_agent.py --check` if that is your selected interpreter. This checks only that URL/key fields load; it does not prove network reachability, authentication, or a model turn. Prove those through the live incoming-message test.
5. Supervise `bridge.py` and `consumer/poll_consumer.py`. Without a usable wake configuration the normal route is incomplete; do not switch to `REPLY_MODE=template` to manufacture success. An explicitly selected slower fallback must actually schedule and wake the agent; writing `pending.json` alone does not.

### Hermes

1. Inspect the installed Hermes version, working provider/model login, current gateway, and service manager. Reuse them. If installation or model login is missing, include it in the prerequisite handoff; do not silently run an installer or replace a working deployment.
2. Generate the manifest with that runtime's `hermes slack manifest --agent-view --write` if creating/updating an app, including when Hermes was already installed. Merge `.env` and `config.yaml` fields from the [Hermes checklist](../hermes/AGENT.md); preserve other platforms and avoid duplicate YAML keys.
3. Configure human authorization, bot-message acceptance, actual channel/peer scope, and reply routing separately. `allow_bots: mentions` alone does not authorize every peer. Check the version's treatment of bot posts with user IDs. A default home channel is not an access-control list.
4. Make channel/thread history and collaboration rules available to the **running turns**. Inspect native support first. The repository's patches target a fixed upstream `836b5f8…` and an ordered prerequisite chain; follow the [version-specific instructions](../hermes/README.md), check existing patches/dirty files, run `git apply --check` before applying a compatible patch, and stop on conflicts. Never force an old patch onto a newer runtime. If compatibility is missing, report the needed adaptation rather than claim full configuration.
5. Use the existing gateway's supported reload/restart path, verify the loaded config/code, and run one gateway instance. Register the actual history-tool path in the loaded agent guidance. Use the native adapter's final response, not a second raw Slack POST. Keep approval mechanisms intact.

### Hosted ChatGPT DOTS

1. Follow the [DOTS checklist](../chatgpt-dots/AGENT.md). Inspect available tools and account settings: Slack read access, reply access, and automatic event triggering are three distinct capabilities.
2. Reuse the existing connection. Use the supported OAuth/consent UI when connection or access is missing. Verify the channel and author identities through that connection.
3. If native events already cover the required bot messages, reuse them. Otherwise create or update **one** supported new-message subscription scoped to the agreed channel/authors and required thread events. Persist the source checks, fresh context, native handoffs, reply routing, and quiet completion rules in its actual execution instructions, then read them back.
4. `config.example.json` is an intent example, not an import format. `reference/` is offline code, not hosted runtime control. Do not invent a private settings file, add cron polling, or install a local bridge inside DOTS.
5. If this account cannot subscribe to the required events or cannot reply to the source, report that precise capability blocker. A manual history read does not prove automatic triggering; do not substitute it during acceptance.

### Make the setup survive the current session

For local routes, render service definitions with the correct absolute interpreter/script/working-directory paths and a persistent writable state location. Muse and Grok both ship `slack-bridge.service`; use distinct service names when colocated, and update Grok consumer dependencies to the chosen bridge name. Preserve the intended service user and proxy/CA environment.

Use the host's existing supervisor. The shipped units are systemd examples, not macOS/Windows installers. Verify actual process ownership, restart policy and consumer health; avoid competing cron/keepalive/systemd owners. Do not clear a queue to make a health check pass. Record how the operator checks status/logs and how the authorized service is reloaded. If persistence across host/container replacement is unavailable, state that limit.

For hosted routes, read back the persisted connection/subscription and its execution instructions. For every route, prove that the actual entry point loads the collaboration rules; editing a file that no running agent reads is insufficient.

## 4. Verify the intended behavior

Use fresh markers in the agreed test channel. Record source/reply timestamps and links privately, plus automatic trigger evidence from the actual runtime. Do not manually wake the responding agent during a test advertised as automatic. Observe before retrying an apparently missing response.

| Check | Procedure | Pass condition |
|---|---|---|
| Effective configuration | Read saved settings and confirm the correct running entry point/ref | Saved and loaded values agree; no duplicate listener/consumer/subscription |
| Real agent | One authorized human DM or native channel mention asks a small, non-template question | Actual model turn and one useful reply at the intended destination |
| Current context | Put a harmless fact in a preceding channel message and a later correction in a test thread; ask using a native mention without repeating the answer | Agent uses the latest applicable facts and replies in the source thread; history/tool access failures are reported |
| Thread events, when required | Have an authorized human reply without a mention under the bot's own test message, if the selected route is configured to support it | The event automatically reaches the real consumer and produces a reply in that same thread; do not infer this from a top-level mention |
| Peer directions | A→B, then B→A, separate fresh markers, at most one reply per probe | Each direction automatically triggers the intended peer once; record coverage per pair |
| Natural collaboration | Give the agreed participants one open task, such as the [shared design task](../README.md#try-a-shared-task), without assigning a speaking order | They organize, review, revise, and deliver a shared result without human relaying; no continued meaningless loop |
| Completion and health | Inspect the route's private processing/send state where exposed and the running service/subscription | Completion evidence retained, failures/uncertainty visible, existing work still healthy |

The pair test's one-reply limit applies to that probe, not to an entire discussion. For a single-agent setup with no available peer, finish and report the single-agent checks; mark collaboration **NOT_EXERCISED** and identify the missing peer. Do not hold completed local work hostage to another platform, or claim full collaboration prematurely. See [acceptance layers](./validation.en.md#acceptance-layers).

## 5. Deliver a usable handoff

Send a short report in the user's language to their authorized private destination:

| Field | Required content |
|---|---|
| Target | Route, runtime/version, repository ref, workspace/channel and intended peers |
| Changes | Private configuration locations or supported settings surfaces, redacted differences, model/hook binding, service/subscription names |
| State | **Configured**, **loaded**, and **verified** separately for each component; overall **complete**, **partial**, or **blocked** with the specific reason |
| Evidence | Test source/reply links, automatic-trigger evidence, context/routing result, counts and observed latency; tested peer directions |
| Operation | Exact private status/log/reload instructions, recovery location, persistence limits, retained uncertain items if any |
| Remaining work | Only genuinely missing user actions/capabilities or untested checks, and the next safe step; never secrets |

For a requested collaboration setup, “complete” means all requested routes and peer checks above pass, not merely that configuration was written. A missing required platform capability makes the overall result partial or blocked. If the user requested only one agent, report that narrower scope explicitly. Never publish filled-in deployment records, credentials, queues, or private chat evidence in this repository.
