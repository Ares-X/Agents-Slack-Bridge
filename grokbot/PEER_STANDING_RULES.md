# PEER_STANDING_RULES.md — @-peer multi-agent standing rules

Standing rules for Grok Bot when other agents @mention you in Slack.
Also summarized in [AGENT.md](./AGENT.md) §多 agent 协作默认. When woken, see [wakeup.md](./wakeup.md) / [AGENT_WAKE.md](./AGENT_WAKE.md).

## Defaults (do not relax unless the user asks)

1. **Allow other bots to @ you**  
   Bridge must **not** drop all bot messages. Only discard **your own** `user_id` (anti self-loop).  
   Tighten with `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS` (comma-separated whitelist) if needed.

2. **Read channel history before every reply**  
   `channel_history.py <channel> [N]` (or equivalent). On failure: visible degrade in log/reply — **never pretend** you had context.

3. **Reply in channel top-level by default**  
   `REPLY_IN_THREAD=0`. If `1`: use existing `thread_ts`, else message `ts`.

4. **Etiquette**
   - When @'d: useful answer; no empty spin / bare echo.
   - **Do not @ yourself**; mention → one reply → stop (no echo / ping-pong storms).
   - When quoting others, turn `@` into plain names to avoid second-hand triggers.
   - Use `ALLOWED_BOT_*` to tighten peers — do **not** disable history reads or revert to “drop all bots”.

5. **One (channel, ts) → one crafted reply**  
   Different peers / channels / questions get different texts. Use  
   `pending_consume_once.py --channel … --ts … --text '…'` — never `--text` alone.

## Quick checks

- Another agent `@Grok Bot` → enqueued, history read, useful **top-level** reply.
- No self-@; no template auto-send when `agent_wake` is on.
