"""Shared claim → send → ack pipeline (consumer + pending fallback).

Rules:
  1. Durable claim (reply_status=sending) BEFORE any send. If claim persist
     fails → do NOT send.
  2. On restart, stale sending → uncertain (escalate_stale_sending); never
     direct resend.
  3. classify_send_result: ONLY auto-retry when output PROVES not sent
     (not_sent: / sent ok: False). Everything else → uncertain.
  4. sent → ack only on later rounds; uncertain/sending → never blind-resend.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from inbox_store import (
    ack_keys,
    claim_for_send,
    escalate_stale_sending,
    is_claimable,
    msg_key,
    set_reply_status,
)

ROOT = os.path.dirname(os.path.abspath(__file__))


def classify_send_result(proc) -> str:
    """Return 'ok' | 'fail' | 'uncertain'.

    fail = proven NOT sent (safe to release claim → retryable).
    uncertain = anything that does not prove absence of a successful post
                (incl. nonzero exit without not_sent / success text).
    """
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    out = stdout + stderr
    if proc.returncode == 0 and "sent ok: True" in stdout:
        return "ok"
    # Proven not sent — safe to retry after releasing claim.
    if "not_sent:" in out:
        return "fail"
    if "sent ok: False" in stdout:
        return "fail"
    # Nonzero exit with no proof → UNCERTAIN (Slack may have accepted then
    # client timed out). Never treat as fail.
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
) -> Dict[str, Any]:
    """Claim → send → mark status → ack.

    Returns dict with keys: action, outcome, detail.
    """
    if escalate_sending:
        escalate_stale_sending(inbox_path)

    key = msg_key(message)
    ch, ts = key
    status = message.get("reply_status")

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
        # sending without escalate: treat as no-resend (verify path).
        return {
            "action": "skip",
            "outcome": "no_resend",
            "detail": status,
        }

    if not is_claimable(status):
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

    # Claim durable — only now may we send.
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
            # Send ok but status persist failed → uncertain (no blind resend).
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

    if outcome == "fail":
        # Proven not sent — release claim to retryable.
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
