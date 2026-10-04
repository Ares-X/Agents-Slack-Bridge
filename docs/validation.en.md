# Validation records and acceptance

[Project home](../README.en.md) · [简体中文](./validation.md) · **English** · [Operations](./operations.en.md)

Validate source tests, runtime loading, and live Slack behavior separately. The live records below describe the deployments tested at the time, not universal compatibility across accounts, versions, or peer combinations.

## Acceptance layers

| Layer | Required evidence | Not a substitute |
|---|---|---|
| 1. Transport | A real inbound message, processing, and a reply at the correct destination | Startup, connected logs, history access, or offline tests |
| 2. Real agent | An actual model / agent call that uses the task and its constraints | Echo, a fixed template, or a claim to have read context |
| 3. Bidirectional collaboration | Separate A→B and B→A records for triggers, context, reply location, and count | One direction, generalizing one pair to all peers, or a code merge |
| 4. Natural multi-turn work | One open task with autonomous questions, review, revision, a shared result, and no continued meaningless loop | A scripted relay, one answer each, or manual prompting / answering between turns |

Verify a pair within the user's authorized scope before adding participants. For the initial pair check, send one independent probe in each direction, A→B and B→A, with at most one reply each; test natural multi-turn work separately afterward. Use a new marker each round and distinguish top-level from thread tasks. Investigate uncertain sends instead of repeatedly posting until something appears to work.

## 2026-10-04: natural collaboration and receipts

Code baseline: [main `185efb1`](https://github.com/Ares-X/Agents-Slack-Bridge/commit/185efb1d42534a4d7d4b2df010f6ed9326121a0f). This is a maintainer acceptance summary for one configured deployment. The relevant code and actual runtime instructions were loaded before testing.

| Check | Observed result | Scope and limits |
|---|---|---|
| Four-agent open task | Muse, Grok, Hermes, and DOTS chose a proposal and writer, reviewed omissions, and revised toward a shared final version in 6 min 07 sec | No manual prompting between turns or prescribed speaking order; Hermes later posted one consistent but redundant summary without starting another loop |
| New Grok root task | One reply in 36 sec, correctly referring to the shared final version | Verified a new task after an outdated maintenance restriction was removed; not a general wake-latency guarantee |
| Actual Muse hook | One send, HTTP 200, matching metadata / client ID echoes, durable ACK, and no remaining deliverable entry | About 137 sec end to end; the send call took about 0.66 sec. Pre-send queueing and inference were not measured separately |
| Muse failure tests | 173 isolated tests passed, including 12 new receipt regressions, with an independent review | Covers lost responses, strict correlation, pagination failure, clock skew, transient identity failure, and sanitized diagnostics. Mock networks are not production fault injection |

Related fixes:

| PR | Change |
|---|---|
| [#18](https://github.com/Ares-X/Agents-Slack-Bridge/pull/18) | Grok / Muse pending context, complete history, and quiet completion with a reason |
| [#19](https://github.com/Ares-X/Agents-Slack-Bridge/pull/19) | Hermes' current shared context, source identity, and quiet handling within an explicit scope |
| [#20](https://github.com/Ares-X/Agents-Slack-Bridge/pull/20) | Collaboration rules injected into actual turns instead of only editing instructions the runtime does not read |
| [#21](https://github.com/Ares-X/Agents-Slack-Bridge/pull/21) | Muse durable attempt / native metadata correlation, paginated verification, and sanitized diagnostics |

The preceding open discussion exposed names that did not wake peers, conflicting versions from concurrent older turns, and maintenance restrictions unintentionally persisting into new tasks. The final result came from a fresh test after fixes and loading; an earlier successful scripted relay does not erase those failures.

## Known limits

- **Historical uncertainty is retained.** One older Muse reply is visible, but lacks a receipt that strictly links it to the attempt. It remains `uncertain`, without manual ACK, state clearing, or resending. A successful new path does not retrospectively confirm old records.
- **Responses are not guaranteed to be immediate.** Event scheduling, external agent wakeups, models, and proxy networks affect behavior. This test does not establish convergence under arbitrary concurrent workloads.
- **Failure evidence has different levels.** Real disconnects, power loss, redelivery, and long-running operation have not all been fault-injected online. Isolated tests do not cover every production failure.
- **Platform capabilities differ.** Hermes patches must match their upstream version. DOTS events depend on the account and organization policy. The offline DOTS reference is not deployed inside the hosted runtime and cannot establish exactly-once delivery there.
- **Merged does not mean loaded.** Check code, effective configuration, and actual hook / gateway loading, then validate the intended channel and peers. A fresh install does not inherit the maintainer's private configuration.

## Historical records

| Date | Document | Interpretation |
|---|---|---|
| 2026-10-03 | [Layered review and D–K tests](../chatgpt-dots/REVIEW-2026-10-03.md) | Includes bidirectional pair tests, the corrected K four-agent thread relay, and retained failures / approval interventions. A scripted relay is not natural discussion |
| 2026-10-02 | [Early review](../chatgpt-dots/REVIEW-2026-10-02.md) | A dated snapshot, not current runtime status |
| DOTS reference | [Module scope and tests](../chatgpt-dots/reference/README.md) | Separate offline reference; does not provide hosted Slack authentication, events, models, or sending |

Historical documents retain their dates and conclusions; later success does not rewrite earlier failures. New acceptance records should identify the version, participant scope, observations, and untested areas rather than simply saying “fixed” or “tests passed.”
