#!/usr/bin/env python3
"""Offline regression for the fixed 836 gateway memory-notification patch.

Usage: python3 test_memory_notifications_platform.py /path/to/patched/upstream
Requires only stdlib. Reads already patched upstream files without modifying them,
compiles both, executes the original resolver and the
entire real callback-wiring method extracted by AST. Unrelated callback owners
are stubs; this does NOT validate the complete gateway runtime or Slack delivery.
"""
import argparse
import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    args = parser.parse_args()
    names = ("display_config.py", "run_turn_runner.py")
    tree = args.upstream
    for name in names:
        source = tree / "gateway" / name
        print(f"source {name} sha256={hashlib.sha256(source.read_bytes()).hexdigest()}")
    resolver_ns = {}
    for name in names:
        path = tree / "gateway" / name
        compiled = compile(path.read_text(), str(path), "exec")
        if name == "display_config.py":
            exec(compiled, resolver_ns)
    resolve = resolver_ns["resolve_display_setting"]
    assert "memory_notifications" in resolver_ns["OVERRIDEABLE_KEYS"]
    runner_ast = ast.parse((tree / "gateway/run_turn_runner.py").read_text())
    cls = next(n for n in runner_ast.body if isinstance(n, ast.ClassDef) and n.name == "TurnRunner")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_wire_turn_agent_callbacks")
    calls = [n for n in ast.walk(cls) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_wire_turn_agent_callbacks"]
    assert len(calls) == 1 and isinstance(calls[0].args[-1], ast.Name) and calls[0].args[-1].id == "platform_key"
    wiring_ns = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<real-836-wiring>", "exec"), wiring_ns)
    wire = wiring_ns[method.name]
    marker = object()
    ctx = NS(source=NS(platform="slack"), progress_callback=marker,
             _voice_ack_guild=[None], _native_slack_task_cards=False,
             _hooks_ref=NS(loaded_hooks=False), _status_callback_sync=marker,
             _event_callback_sync=marker, session_key="test", _status_adapter=None,
             resolve_display_setting=resolve, _thinking_enabled=False,
             mute_notification_reply=False, agent_holder=[None], tools_holder=[None],
             process_task_id="test", process_baseline=set())
    owner = NS(_ctx=ctx, _runner=NS(_service_tier=None, _consume_pending_turn_sidecar_notes=lambda key: []),
               _notice_callback_sync=marker, _clarify_callback_sync=marker,
               _merge_turn_request_overrides=lambda *a: None,
               _make_bg_review_callbacks=lambda: (marker, marker),
               _attach_session_title_callback=lambda *a: None)
    # One reused agent for all cases: next-turn reassignment is exercised directly.
    agent = NS()
    cases = [
        ("Slack platform off", {"display": {"memory_notifications": "on", "platforms": {"slack": {"memory_notifications": "off"}}}}, "slack", "off"),
        ("same agent Telegram global on", {"display": {"memory_notifications": "on", "platforms": {"slack": {"memory_notifications": "off"}}}}, "telegram", "on"),
        ("other platform global verbose", {"display": {"memory_notifications": "verbose"}}, "discord", "verbose"),
        ("platform false", {"display": {"memory_notifications": "verbose", "platforms": {"slack": {"memory_notifications": False}}}}, "slack", "off"),
        ("platform true", {"display": {"memory_notifications": "off", "platforms": {"slack": {"memory_notifications": True}}}}, "slack", "on"),
        ("global false", {"display": {"memory_notifications": False}}, "telegram", "off"),
        ("global true", {"display": {"memory_notifications": True}}, "telegram", "on"),
        ("missing default", {}, "slack", "on"),
        ("null display", {"display": None}, "slack", "on"),
        ("null global", {"display": {"memory_notifications": None}}, "slack", "on"),
        ("null platform inherits verbose", {"display": {"memory_notifications": "verbose", "platforms": {"slack": {"memory_notifications": None}}}}, "slack", "verbose"),
        ("Slack off before config restoration", {"display": {"platforms": {"slack": {"memory_notifications": "off"}}}}, "slack", "off"),
        ("same agent next turn restored", {"display": {"memory_notifications": "on"}}, "slack", "on"),
    ]
    for label, config, platform, expected in cases:
        ctx.user_config = config
        ctx.source.platform = platform
        wire(owner, agent, None, None, None, None, False, platform)
        assert agent.memory_notifications == expected, (label, agent.memory_notifications, expected)
        assert agent.background_review_callback is marker, label
        assert agent.tool_progress_callback is marker and agent.event_callback is marker, label
        assert agent.notice_callback is marker and agent.clarify_callback is marker, label
        print(f"PASS {label}: {expected}")
    print(f"PASS compile 2 files; {len(cases)} real resolver/wiring cases")
    print("NOT_EXERCISED: complete gateway runtime, background review execution, Slack delivery")


if __name__ == "__main__":
    main()
