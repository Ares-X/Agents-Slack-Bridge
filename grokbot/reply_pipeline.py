"""Shared claim → send → ack pipeline (consumer + pending fallback).

Rules:
  1. Durable claim (reply_status=sending) BEFORE any send. If claim persist
     fails → do NOT send.
  2. On restart, stale sending → uncertain (escalate_stale_sending); never
     direct resend.
  3. classify_send_result:
       - ok / fail (proven not_sent) / rate_limited / uncertain
       - rate_limited → wait until retry_after_until, then retry (no hammer)
       - internal_error/fatal_error/unknown → uncertain (no auto-resend)
  4. sent → ack only on later rounds; uncertain/sending → never blind-resend.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Dict, Optional

from inbox_store import (
    ack_keys,
    claim_for_send,
    escalate_stale_sending,
    is_claimable,
    is_send_ready,
    msg_key,
    set_reply_status,
)

ROOT = os.path.dirname(os.path.abspath(__file__))

_AMBIGUOUS_SLACK_API = frozenset({"internal_error", "fatal_error"})
_DEFAULT_RETRY_AFTER = 60.0
_RETRY_AFTER_RE = re.compile(
    r"rate_limited:\s*retry_after=([0-9]+(?:\.[0-9]+)?)", re.I
)


def parse_retry_after_seconds(text: str, default: float = _DEFAULT_RETRY_AFTER) -> float:
    m = _RETRY_AFTER_RE.search(text or "")
    if not m:
        return float(default)
    try:
        return max(0.0, float(m.group(1)))
    except ValueError:
        return float(default)


def classify_send_result(proc) -> str:
    """Return 'ok' | 'fail' | 'rate_limited' | 'uncertain'."""
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    out = stdout + stderr
    if proc.returncode == 0 and "sent ok: True" in stdout:
        return "ok"
    # Rate limit before ambiguous / not_sent checks.
    if "rate_limited:" in out or proc.returncode == 3:
        # Don't treat as uncertain even if send_error also present.
        if "rate_limited:" in out or "ratelimited" in out.lower():
            return "rate_limited"
    for code in _AMBIGUOUS_SLACK_API:
        if f"slack_api {code}" in out:
            return "uncertain"
    if "not_sent:" in out:
        return "fail"
    if "sent ok: False" in stdout and "send_error:" not in out and "rate_limited:" not in out:
        return "fail"
    return "uncertain"


def run_send(
    channel: str,
    text: str,
    *,
    thread_ts: Optional[str] = None,
    root: str = ROOT,
    python: str = sys.executable,
    runner: Optional[Callable[..., Any]] = None,
):
    """Invoke send.py; runner override for tests."""
    cmd = [python, "send.py", channel]
    if thread_ts:
        cmd += ["--thread-ts", thread_ts]
    if runner is not None:
        return runner(*cmd, input_text=text)
    return subprocess.run(
        cmd, input=text, capture_output=True, text=True, cwd=root
    )


def process_one(
    inbox_path: str,
    message: dict,
    reply_text: str,
    *,
    reply_in_thread: bool = False,
    thread_ts: Optional[str] = None,
    root: str = ROOT,
    runner: Optional[Callable[..., Any]] = None,
    escalate_sending: bool = False,
    time_fn: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Claim → send → mark status → ack.

    time_fn is injectable for rate-limit wait tests (mocked clock).
    """
    if escalate_sending:
        escalate_stale_sending(inbox_path)

    key = msg_key(message)
    ch, ts = key
    status = message.get("reply_status")
    now = float(time_fn())

    if message.get("delivered"):
        return {"action": "skip", "outcome": "already_delivered"}

    if status == "sent":
        n, missing = ack_keys(inbox_path, {key})
        ok = n > 0 and not missing
        return {
            "action": "ack_only",
            "outcome": "acked" if ok else "ack_failed",
        }

    if status in ("uncertain", "sending"):
        return {
            "action": "skip",
            "outcome": "no_resend",
            "detail": status,
        }

    if status == "rate_limited":
        try:
            until = float(message.get("retry_after_until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        if now < until:
            return {
                "action": "skip",
                "outcome": "wait_rate_limit",
                "retry_after_until": until,
                "wait_sec": until - now,
            }
        # Wait expired — fall through to claim+send (is_send_ready True).

    if not is_send_ready(message, now=now) and not is_claimable(status):
        return {"action": "skip", "outcome": "not_claimable", "detail": status}

    token = uuid.uuid4().hex
    try:
        claimed = claim_for_send(inbox_path, key, token)
    except Exception as e:
        return {
            "action": "skip",
            "outcome": "claim_persist_failed",
            "detail": str(e),
        }
    if not claimed:
        return {"action": "skip", "outcome": "claim_lost"}

    tt = None
    if reply_in_thread:
        tt = (thread_ts or message.get("thread_ts") or message.get("ts") or "").strip() or None

    try:
        proc = run_send(ch, reply_text, thread_ts=tt, root=root, runner=runner)
        outcome = classify_send_result(proc)
    except Exception as e:
        set_reply_status(
            inbox_path, {key}, "uncertain",
            extra_fields={"uncertain_reason": f"send_exception:{e}"},
        )
        return {"action": "send", "outcome": "uncertain", "detail": str(e)}

    if outcome == "ok":
        n, missing = set_reply_status(inbox_path, {key}, "sent")
        if n == 0 or missing:
            try:
                set_reply_status(
                    inbox_path, {key}, "uncertain",
                    extra_fields={"uncertain_reason": "sent_status_persist_failed"},
                )
            except Exception:
                pass
            return {
                "action": "send",
                "outcome": "uncertain",
                "detail": "status_persist_failed_after_ok",
            }
        an, am = ack_keys(inbox_path, {key})
        return {
            "action": "send",
            "outcome": "sent_acked" if an > 0 and not am else "sent_ack_pending",
            "stdout": proc.stdout,
        }

    if outcome == "rate_limited":
        out = (proc.stdout or "") + (proc.stderr or "")
        sec = parse_retry_after_seconds(out)
        until = float(time_fn()) + sec
        set_reply_status(
            inbox_path, {key}, "rate_limited",
            extra_fields={
                "retry_after_sec": sec,
                "retry_after_until": until,
                "last_send_error": out[:300],
            },
        )
        return {
            "action": "send",
            "outcome": "rate_limited",
            "retry_after_sec": sec,
            "retry_after_until": until,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }

    if outcome == "fail":
        set_reply_status(
            inbox_path, {key}, "retryable",
            extra_fields={"last_send_error": (proc.stderr or proc.stdout or "")[:300]},
        )
        return {
            "action": "send",
            "outcome": "fail_retryable",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }

    set_reply_status(
        inbox_path, {key}, "uncertain",
        extra_fields={
            "uncertain_reason": (
                f"rc={proc.returncode} out={(proc.stdout or '')[:120]!r} "
                f"err={(proc.stderr or '')[:120]!r}"
            ),
        },
    )
    return {
        "action": "send",
        "outcome": "uncertain",
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }
