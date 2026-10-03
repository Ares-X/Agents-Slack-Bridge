#!/usr/bin/env python3
"""Read-only behavior checks against fixed 836 + PR17 + PR19 + turn-contract patch.

Executes complete production methods with isolated transport/runtime owners.
No gateway startup, network, credentials, or upstream writes.
"""
import argparse
import asyncio
import hashlib
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import patch

from test_natural_collaboration import extract


async def checks(root):
    paths = [root / "plugins/platforms/slack/adapter.py",
             root / "gateway/platforms/base.py", root / "gateway/run_turn_runner.py",
             root / "gateway/run_turn.py"]
    ns = dict(MessageEvent=lambda **kw: NS(internal=False, **kw), MessageType=NS(COMMAND="command"),
              ProcessingOutcome=NS(SUCCESS="success", FAILURE="failure"))
    for path in paths:
        compile(path.read_text(), str(path), "exec")
        print(f"source {path.relative_to(root)} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
    extract(paths[0], ["_channel_prompt_with_identity", "_build_message_event"], ns)
    extract(paths[1], ["resolve_channel_prompt", "resolve_channel_skills"], ns)
    extract(paths[2], ["_combined_ephemeral_prompt"], ns)
    extract(paths[3], ["_run_agent_queued_followup"], ns)
    ns.update(logger=logging.getLogger("isolated-turn-contract"), display_kind_for_event=lambda ev: None,
              diagnostic_metadata=lambda ev: {})
    base = ModuleType("gateway.platforms.base")
    base.resolve_channel_prompt = ns["resolve_channel_prompt"]
    base.resolve_channel_skills = ns["resolve_channel_skills"]
    gateway = ModuleType("gateway"); gateway.__path__ = []
    platforms = ModuleType("gateway.platforms"); platforms.__path__ = []
    count = 0

    def check(label, condition):
        nonlocal count
        assert condition, label
        count += 1
        print(f"PASS {label}")

    adapter = NS(config=NS(extra={"channel_prompts": {"C1": "Existing channel guidance"},
                                 "collaboration_channels": {"T1:C1": {}}}),
                 _build_identity_prompt=lambda team: "Existing identity")
    prompt_method = ns["_channel_prompt_with_identity"]
    adapter._channel_prompt_with_identity = lambda *a, **kw: prompt_method(adapter, *a, **kw)
    marker = "[Slack collaboration turn contract]"
    original = "Existing identity\n\nExisting channel guidance"
    with patch.dict(sys.modules, {"gateway": gateway, "gateway.platforms": platforms,
                                 "gateway.platforms.base": base}):
        prompt = adapter._channel_prompt_with_identity("C1", "T1", chat_type="group")
        check("configured group gets trusted contract after unchanged identity/channel prefix",
              prompt.startswith(original + "\n\n" + marker) and prompt.count(marker) == 1)
        for label, channel, team, kind in [
            ("DM even with matching scope", "C1", "T1", "dm"),
            ("other workspace", "C1", "T2", "group"),
            ("other channel", "C2", "T1", "group"),
            ("missing workspace", "C1", "", "group"),
            ("missing chat classification", "C1", "T1", None),
        ]:
            expected = original if channel == "C1" else "Existing identity"
            check(label + " retains default prompt", adapter._channel_prompt_with_identity(
                channel, team, chat_type=kind) == expected)
        for label, scopes in [("absent scope map", None), ("malformed map", []),
                              ("malformed scope options", {"T1:C1": True})]:
            adapter.config.extra["collaboration_channels"] = scopes
            check(label + " retains default prompt", adapter._channel_prompt_with_identity(
                "C1", "T1", chat_type="group") == original)
        adapter.config.extra["collaboration_channels"] = {"T1:C1": {}}
        adapter._build_identity_prompt = lambda team: ""
        adapter.config.extra["channel_prompts"] = {}
        check("configured group without prior prompt still gets contract",
              adapter._channel_prompt_with_identity("C1", "T1", chat_type="group").startswith(marker))
        check("unconfigured default without identity remains None",
              adapter._channel_prompt_with_identity("C2", "T1", chat_type="group") is None)
        adapter._build_identity_prompt = lambda team: "Existing identity"
        adapter.config.extra["channel_prompts"] = {"C1": "Existing channel guidance"}

        async def name(*a, **kw):
            return "cached name"
        async def humanize(text, **kw):
            return text
        adapter._resolve_user_name = adapter._resolve_channel_name = name
        adapter._humanize_user_mentions = humanize
        adapter._media_message_type = lambda media: "text"
        adapter._event_declares_bot_sender = lambda event: bool(event.get("bot_id"))
        adapter.build_source = lambda **kw: NS(**kw)
        events = []
        for is_dm in (False, True):
            event = await ns["_build_message_event"](
                adapter, {"bot_id": "BPEER"}, text="peer request", original_text="peer request",
                command_probe_text="peer request", is_command_text=False, channel_id="C1",
                team_id="T1", ts="100.000", user_id="UPEER", thread_ts=None, is_dm=is_dm,
                media_urls=[], media_types=[], media_text_inlined=[], channel_context="untrusted observations")
            events.append(event)
            check(("DM" if is_dm else "group") + " production event builder carries correct contract scope",
                  (marker in event.channel_prompt) == (not is_dm))
            check(("DM" if is_dm else "group") + " keeps author, body and observed data separate",
                  event.source.user_id == "UPEER" and event.text == "peer request"
                  and event.channel_context == "untrusted observations"
                  and "untrusted observations" not in event.channel_prompt)

        # This owner is used on every executor entry, including cached agents and queued turns.
        runner = NS(_get_system_prompt_for_channel=lambda *a, **kw: "Existing system override")
        ctx = NS(context_prompt="Existing platform context", channel_prompt=events[0].channel_prompt,
                 source=NS(platform="slack", chat_id="C1", thread_id=None))
        turn = NS(_ctx=ctx, _runner=runner)
        combined = ns["_combined_ephemeral_prompt"](turn)
        check("real runner assembles contract in system context without observed text",
              combined == "Existing platform context\n\n" + prompt + "\n\nExisting system override")
        check("repeated existing-session executor entry retains one static contract",
              ns["_combined_ephemeral_prompt"](turn) == combined and combined.count(marker) == 1)
        ctx.channel_prompt = events[1].channel_prompt
        check("next DM executor entry does not inherit group contract",
              marker not in ns["_combined_ephemeral_prompt"](turn))

        # Execute the real queued owner: the pending event's trusted prompt must win over
        # the opener's. Transport/delivery are inert, and authors stay distinct.
        run = ModuleType("gateway.run")
        run._preserve_queued_followup_history_offset = lambda prior, followup: dict(followup)
        base.merge_pending_message_event = lambda *a, **kw: None
        inbound = ModuleType("gateway.run_inbound")
        inbound.strip_discord_triggering_note = lambda ev, text: text
        ack = ModuleType("gateway.run_turn_followup_ack")
        async def no_op(*a, **kw):
            return None
        ack._run_followup_processing_hook = no_op
        ack._followup_cancel_outcome = lambda adapter: "cancelled"
        captured = []
        async def run_followup(**kw):
            captured.append(kw)
            return {"final_response": "done", "messages": []}
        async def prepare_followup(**kw):
            return kw["event"].text
        owner = NS(_MAX_INTERRUPT_DEPTH=5, _run_agent_deliver_first_response=no_op,
                   _is_goal_continuation_event=lambda ev: False,
                   _session_key_for_source=lambda src: "author-key:" + src.user_id,
                   _prepare_profile_scoped_inbound_message_text=prepare_followup,
                   _reply_anchor_for_event=lambda ev: ev.message_id,
                   _delivery_adapter_for=lambda src: None, _intake_adapter_for=lambda src: None,
                   _refresh_agent_cache_message_count=no_op, _run_agent=run_followup)
        opener = NS(source=NS(user_id="UOPENER"), session_id="sid", session_key="opener-key",
                    run_generation=1, _interrupt_depth=0, history=[], _status_thread_metadata={},
                    context_prompt="Existing platform context", result_holder=[None])
        with patch.dict(sys.modules, {"gateway.run": run, "gateway.run_inbound": inbound,
                                     "gateway.run_turn_followup_ack": ack}):
            for next_event in events:
                await ns["_run_agent_queued_followup"](
                    owner, opener, None, "pending", next_event,
                    {"final_response": "prior"}, {"messages": []}, None)
                call = captured[-1]
                check("queued " + next_event.source.chat_type + " receives its own trusted prompt and author",
                      call["channel_prompt"] == next_event.channel_prompt
                      and call["source"] is next_event.source
                      and call["session_key"] == "author-key:UPEER"
                      and (marker in call["channel_prompt"]) == (next_event.source.chat_type == "group"))
    print(f"PASS compile {len(paths)} files; {count} isolated behavior checks")
    print("NOT_EXERCISED: complete gateway, loaded code, model compliance, live concurrent conversation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    asyncio.run(checks(parser.parse_args().upstream))


if __name__ == "__main__":
    main()
