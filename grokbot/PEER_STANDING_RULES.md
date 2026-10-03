# PEER_STANDING_RULES.md — @-peer multi-agent standing rules

Standing rules for Grok Bot when other agents @mention you in Slack.
Also summarized in [AGENT.md](./AGENT.md) §多 agent 协作默认. When woken, see [wakeup.md](./wakeup.md) / [AGENT_WAKE.md](./AGENT_WAKE.md).

## Defaults (do not relax unless the user asks)

1. **Allow other bots to @ you**  
   Bridge must **not** drop all bot messages. Only discard **your own** `user_id` (anti self-loop).  
   Tighten with `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS` (comma-separated whitelist) if needed.

2. **Read pending work and current task context before deciding**
   Read related pending rows together by channel/thread, the latest authorized
   task, and later constraints. Use `channel_history.py <channel> [N]` and
   `--thread-ts <ROOT_TS> --all` for relevant threads; `--thread` is an alias.
   Follow pagination with `--all` / `--cursor` when needed. On failure: record
   the limitation — **never pretend** you had context or infer a quiet decision.

3. **Reply in channel top-level by default**  
   `REPLY_IN_THREAD=0`. If `1`: use existing `thread_ts`, else message `ts`.

4. **Etiquette**
   - Reply if it advances the authorized task. Notifications, pure confirmations,
     approval reminders and superseded control messages may be resolved quietly
     with a durable reason. Do not send a confirmation of every confirmation.
   - Include `<@USER_ID>` **only to request a peer's concrete next action**.
     Use plain names for references, acknowledgements and status reports.
   - **Do not @ yourself**. Continue useful authorized discussion across turns;
     stop echo/control loops, not an entire task after its first substantive reply.
   - When quoting others, turn `@` into plain names to avoid second-hand triggers.
   - Use `ALLOWED_BOT_*` to tighten peers — do **not** disable history reads or revert to “drop all bots”.

5. **One (channel, ts) → at most one reply, or a reasoned quiet decision**
   Use `pending_consume_once.py --channel … --ts … --text '…'`, or
   `--no-reply --reason '…'`. Never broadcast one text/decision across rows.
   Quiet resolution retains the original row, reason and time; it only accepts
   unsent claimable rows. ACK is restricted to `sent`; no decision clears an
   in-flight, uncertain or rate-limited send.

## Quick checks

- Another agent `@Grok Bot` → enqueued, related pending/task/thread context read,
  then useful reply in the correct location or durable quiet resolution.
- Concrete peer handoff includes `<@USER_ID>`; a confirmation does not wake them.
  No self-@. No template auto-send when `agent_wake` is on.
