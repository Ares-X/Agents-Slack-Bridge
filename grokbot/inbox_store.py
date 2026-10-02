"""Durable inbox.jsonl helpers: append / claim / ack under flock.

Ack identity is (channel, ts) — same as dedupe. Rewrites use temp + os.replace
while holding an exclusive lock on sidecar `inbox.jsonl.lock`.

Durability rules:
  - Hold lock until flush/fsync (+ rename / dir fsync) completes.
  - Directory fsync errors are NOT swallowed (no false success).
  - Duplicate path reconfirms durability (fsync file+dir) before returning False.
  - Truncated last line (no trailing newline) is quarantined under lock; never
    concatenate a new JSON object into a corrupt tail and report success.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

AckKey = Tuple[str, str]  # (channel, ts)

# reply_status values
#   None/""/"retryable" — claimable for send
#   "rate_limited"      — wait until retry_after_until, then claimable again
#   "sending"           — durable in-flight claim (never blind-resend on restart)
#   "sent"              — Slack send confirmed; ack only
#   "uncertain"         — send outcome unknown; never blind-resend
STATUS_CLAIMABLE = frozenset({None, "", "retryable"})
STATUS_NO_RESEND = frozenset({"sending", "sent", "uncertain"})


class DurabilityError(OSError):
    """Raised when durability cannot be confirmed (fsync/dir fsync/verify)."""


def msg_key(record: dict) -> AckKey:
    return (str(record.get("channel") or ""), str(record.get("ts") or ""))


def is_claimable(status) -> bool:
    return status in STATUS_CLAIMABLE or status is None


def is_send_ready(record: dict, now: Optional[float] = None) -> bool:
    """True if this undelivered row may be claimed for send right now.

    rate_limited rows become ready only after retry_after_until (absolute epoch).
    """
    if record.get("delivered"):
        return False
    st = record.get("reply_status")
    if is_claimable(st):
        return True
    if st == "rate_limited":
        try:
            until = float(record.get("retry_after_until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        tnow = time.time() if now is None else float(now)
        return tnow >= until
    return False


def parse_ack_argv(argv: Sequence[str]) -> Set[AckKey]:
    """Parse CLI args as channel+ts pairs (channel ts | channel:ts)."""
    keys: Set[AckKey] = set()
    i = 0
    args = list(argv)
    while i < len(args):
        a = args[i]
        if ":" in a:
            ch, _, ts = a.partition(":")
            if not ch or not ts:
                raise ValueError(f"invalid channel:ts token: {a!r}")
            keys.add((ch, ts))
            i += 1
            continue
        if i + 1 < len(args):
            keys.add((a, args[i + 1]))
            i += 2
            continue
        raise ValueError(
            f"ack identity requires channel+ts pairs; leftover arg: {a!r}"
        )
    return keys


def _lock_path(path: str) -> str:
    return path + ".lock"


def _corrupt_path(path: str) -> str:
    return path + ".corrupt"


def _acquire_lock(path: str, exclusive: bool = True):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    lf = open(_lock_path(path), "a+", encoding="utf-8")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except Exception:
        lf.close()
        raise
    return lf


def _release_lock(lf) -> None:
    try:
        fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        lf.close()


def _fsync_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def fsync_file_and_dir(path: str) -> None:
    """Confirm durability of existing path. Raises DurabilityError on failure."""
    if not os.path.exists(path):
        raise DurabilityError(f"missing file for fsync: {path}")
    try:
        with open(path, "r+", encoding="utf-8") as f:
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(path)
    except OSError as e:
        raise DurabilityError(f"fsync failed for {path}: {e}") from e


def _parse_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except Exception:
        return None


def _quarantine_bytes(path: str, blob: bytes) -> None:
    """Append corrupt evidence to sidecar; fsync. Caller holds exclusive lock."""
    if not blob:
        return
    cpath = _corrupt_path(path)
    with open(cpath, "ab") as cf:
        cf.write(b"\n--- corrupt tail ---\n")
        cf.write(blob)
        if not blob.endswith(b"\n"):
            cf.write(b"\n")
        cf.flush()
        os.fsync(cf.fileno())
    try:
        _fsync_dir(cpath)
    except OSError as e:
        raise DurabilityError(f"corrupt sidecar dir fsync failed: {e}") from e


def read_records_repair_tail(path: str) -> Tuple[List[dict], bool]:
    """Read records; if last line lacks trailing newline, quarantine + repair.

    Returns (valid_records, repaired). Caller must hold exclusive lock when
    repaired may be True (mutates file). For shared/read-only callers, use
    read_records_readonly instead.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return [], False
    with open(path, "rb") as f:
        data = f.read()
    repaired = False
    if not data.endswith(b"\n"):
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            good, corrupt = b"", data
        else:
            good, corrupt = data[: last_nl + 1], data[last_nl + 1 :]
        _quarantine_bytes(path, corrupt)
        # Atomic repair: write good bytes to temp + fsync + replace.
        # Original queue file stays intact until replace succeeds.
        tmp = (
            path
            + ".repair."
            + str(os.getpid())
            + "."
            + uuid.uuid4().hex[:8]
        )
        try:
            with open(tmp, "wb") as out:
                out.write(good)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
            _fsync_dir(path)
        except OSError as e:
            raise DurabilityError(
                f"corrupt-tail repair failed (original preserved): {e}"
            ) from e
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        data = good
        repaired = True
    rows: List[dict] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        rec = _parse_line(line)
        if rec is not None:
            rows.append(rec)
        # Unparseable full lines: leave in file as evidence; skip for logic.
        # (They still occupy a line and won't concatenate on next append.)
    return rows, repaired


def read_records_readonly(path: str) -> List[dict]:
    """Parse valid JSON lines; does not repair. Skips corrupt lines."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    rows: List[dict] = []
    with open(path, "rb") as f:
        data = f.read()
    # If truncated tail, do not treat the partial as a record (and do not
    # silently merge). Exclude the incomplete last segment from parsing.
    if data and not data.endswith(b"\n"):
        last_nl = data.rfind(b"\n")
        data = data[: last_nl + 1] if last_nl != -1 else b""
    for line in data.decode("utf-8", errors="replace").splitlines():
        rec = _parse_line(line)
        if rec is not None:
            rows.append(rec)
    return rows


def _atomic_rewrite(path: str, records: List[dict]) -> None:
    """Write records via temp + os.replace. Dir fsync errors propagate."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp = path + ".tmp." + str(os.getpid()) + "." + uuid.uuid4().hex[:8]
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            for r in records:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        _fsync_dir(path)
    except OSError as e:
        raise DurabilityError(f"atomic rewrite failed for {path}: {e}") from e
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _verify_last_record(path: str, key: AckKey) -> None:
    """Ensure last line is valid JSON for key. Raises DurabilityError."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.endswith(b"\n"):
        raise DurabilityError("post-append file lacks trailing newline")
    lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        raise DurabilityError("post-append file empty")
    rec = _parse_line(lines[-1])
    if rec is None or msg_key(rec) != key:
        raise DurabilityError(
            f"post-append verify failed: last={lines[-1][:200]!r} want={key}"
        )


def append_record(path: str, record: dict) -> bool:
    """Append one record under exclusive lock. Skip dups on (channel, ts).

    Returns True if newly enqueued and durability verified.
    Returns False only if duplicate AND durability reconfirmed (fsync).
    Raises DurabilityError on fsync/verify failure (caller must NOT ACK).
    """
    key = msg_key(record)
    lf = _acquire_lock(path, exclusive=True)
    try:
        rows, _ = read_records_repair_tail(path)
        for r in rows:
            if msg_key(r) == key:
                # Reconfirm durability before telling caller "safe to ACK".
                fsync_file_and_dir(path)
                return False
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as e:
                raise DurabilityError(f"append fsync failed: {e}") from e
        try:
            _fsync_dir(path)
        except OSError as e:
            raise DurabilityError(f"append dir fsync failed: {e}") from e
        _verify_last_record(path, key)
        return True
    finally:
        _release_lock(lf)


def peek_undelivered(path: str) -> List[dict]:
    """Return undelivered records (shared lock). Excludes truncated tail bytes."""
    if not os.path.exists(path) and not os.path.exists(_lock_path(path)):
        return []
    lf = _acquire_lock(path, exclusive=False)
    try:
        return [r for r in read_records_readonly(path) if not r.get("delivered")]
    finally:
        _release_lock(lf)


def peek_claimable(path: str, now: Optional[float] = None) -> List[dict]:
    """Undelivered rows ready to send (claimable or rate_limited wait expired)."""
    out = []
    for r in peek_undelivered(path):
        if is_send_ready(r, now=now):
            out.append(r)
    return out


def peek_actionable(path: str) -> List[dict]:
    """Undelivered rows for fallback: claimable, rate_limited, or sent (ack-only).

    Excludes sending/uncertain (no auto-resend). rate_limited is included so
    process_one can honor wait (skip) or send after expiry — no hammering.
    """
    out = []
    for r in peek_undelivered(path):
        st = r.get("reply_status")
        if is_claimable(st) or st in ("sent", "rate_limited"):
            out.append(r)
    return out


def update_records(
    path: str,
    keys: Iterable[AckKey],
    *,
    delivered: Optional[bool] = None,
    reply_status: Optional[str] = None,
    extra_fields: Optional[Dict[str, Any]] = None,
    only_if_status_in: Optional[Set] = None,
) -> Tuple[int, Set[AckKey]]:
    """Update matching rows. Returns (n_matched, missing_keys).

    If only_if_status_in is set, a row only matches when its reply_status is in
    that set (used for atomic claim). Durability failures raise.
    """
    want = {(str(c), str(t)) for c, t in keys}
    if not want:
        return 0, set()

    lf = _acquire_lock(path, exclusive=True)
    try:
        if not os.path.exists(path):
            return 0, want
        rows, _ = read_records_repair_tail(path)
        found: Set[AckKey] = set()
        dirty = False
        for r in rows:
            k = msg_key(r)
            if k not in want:
                continue
            if only_if_status_in is not None:
                st = r.get("reply_status")
                if st not in only_if_status_in and not (
                    st is None and None in only_if_status_in
                ):
                    # Also allow "" if "" in set
                    if st not in only_if_status_in:
                        continue
            found.add(k)
            if delivered is not None and r.get("delivered") != delivered:
                r["delivered"] = delivered
                dirty = True
            if reply_status is not None and r.get("reply_status") != reply_status:
                r["reply_status"] = reply_status
                dirty = True
            if extra_fields:
                for ek, ev in extra_fields.items():
                    if r.get(ek) != ev:
                        r[ek] = ev
                        dirty = True
        if dirty:
            _atomic_rewrite(path, rows)
        elif found:
            # Matched but already in desired state (e.g. re-ack after rename
            # where dir fsync previously failed). Still must confirm durability;
            # persistent fsync failure must NOT report success.
            fsync_file_and_dir(path)
        # missing = requested keys not successfully matched (absent or gated)
        return len(found), want - found
    finally:
        _release_lock(lf)


def claim_for_send(
    path: str, key: AckKey, claim_token: Optional[str] = None
) -> bool:
    """Atomically claim a message for sending (reply_status → sending).

    Only succeeds if undelivered and is_send_ready (incl. rate_limited after wait).
    If durable persist fails, raises — caller must NOT send.
    Returns True iff this caller owns the claim.
    """
    token = claim_token or uuid.uuid4().hex
    return _claim_for_send_locked(path, key, token)


def _claim_for_send_locked(path: str, key: AckKey, token: str) -> bool:
    lf = _acquire_lock(path, exclusive=True)
    try:
        if not os.path.exists(path):
            return False
        rows, _ = read_records_repair_tail(path)
        hit = None
        for r in rows:
            if msg_key(r) == key:
                hit = r
                break
        if hit is None or hit.get("delivered"):
            return False
        if not is_send_ready(hit):
            return False
        hit["reply_status"] = "sending"
        hit["claim_id"] = token
        hit["claim_at"] = time.time()
        # Clear prior rate-limit fields once we re-claim after wait.
        hit.pop("retry_after_until", None)
        hit.pop("retry_after_sec", None)
        _atomic_rewrite(path, rows)
        return True
    finally:
        _release_lock(lf)


def set_reply_status(
    path: str,
    keys: Iterable[AckKey],
    status: str,
    *,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Set[AckKey]]:
    return update_records(
        path, keys, reply_status=status, extra_fields=extra_fields
    )


def ack_keys(path: str, keys: Iterable[AckKey]) -> Tuple[int, Set[AckKey]]:
    return update_records(path, keys, delivered=True)


def escalate_stale_sending(path: str) -> int:
    """Mark in-flight 'sending' as 'uncertain' (restart/verify path; never resend)."""
    lf = _acquire_lock(path, exclusive=True)
    try:
        if not os.path.exists(path):
            return 0
        rows, _ = read_records_repair_tail(path)
        n = 0
        for r in rows:
            if not r.get("delivered") and r.get("reply_status") == "sending":
                r["reply_status"] = "uncertain"
                r["uncertain_reason"] = "stale_sending_on_restart"
                n += 1
        if n:
            _atomic_rewrite(path, rows)
        return n
    finally:
        _release_lock(lf)
