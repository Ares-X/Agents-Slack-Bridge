"""File-backed cache of bot-sent message timestamps.

Used by bridge.py to accept thread replies under our messages without an
API round-trip when possible. send.py records successful chat.postMessage ts.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Iterable, Optional, Set, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(BASE, "bot_sent_ts.json")
_LOCK = threading.Lock()
_MAX_KEYS = 5000


def _key(channel: str, ts: str) -> str:
    return f"{channel}:{ts}"


def _load(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data if x]
        if isinstance(data, dict) and "keys" in data:
            return [str(x) for x in data["keys"] if x]
    except Exception:
        return []
    return []


def _save(path: str, keys: list) -> None:
    # Keep newest _MAX_KEYS (list append order = oldest→newest)
    trimmed = keys[-_MAX_KEYS:]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"keys": trimmed}, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remember(channel: str, ts: str, path: str = DEFAULT_PATH) -> None:
    """Record that we posted (channel, ts). Idempotent."""
    ch = (channel or "").strip()
    t = (ts or "").strip()
    if not ch or not t:
        return
    k = _key(ch, t)
    with _LOCK:
        keys = _load(path)
        if k in keys:
            return
        keys.append(k)
        _save(path, keys)


def remember_many(pairs: Iterable[Tuple[str, str]], path: str = DEFAULT_PATH) -> None:
    with _LOCK:
        keys = _load(path)
        known = set(keys)
        changed = False
        for channel, ts in pairs:
            ch = (channel or "").strip()
            t = (ts or "").strip()
            if not ch or not t:
                continue
            k = _key(ch, t)
            if k not in known:
                keys.append(k)
                known.add(k)
                changed = True
        if changed:
            _save(path, keys)


def contains(channel: str, ts: str, path: str = DEFAULT_PATH) -> bool:
    ch = (channel or "").strip()
    t = (ts or "").strip()
    if not ch or not t:
        return False
    with _LOCK:
        return _key(ch, t) in set(_load(path))


def load_set(path: str = DEFAULT_PATH) -> Set[str]:
    with _LOCK:
        return set(_load(path))
