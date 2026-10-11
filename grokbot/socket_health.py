"""Socket Mode health heartbeat + external probe (no agent wake).

bridge.py writes socket_heartbeat when the live Socket Mode session has a
fresh ping/pong. keepalive.sh calls probe_cli() every tick: silence of Slack
events is NOT death; a stale/missing heartbeat plus a successful auth.test
means the long connection is dead while the Web API still works → restart
bridge.py only. auth.test failure → log and do not thrash-restart.

Missing heartbeat right after bridge start is OK for SOCKET_PROBE_STALE_SEC
seconds (bridge uptime via /proc); that avoids restart storms before the
first pong arrives.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HEARTBEAT_PATH = os.path.join(BASE, "socket_heartbeat")
DEFAULT_ENV_PATH = os.path.join(BASE, ".env")

# Slightly above SDK stale threshold (ping_interval 5s * 4 = 20s).
DEFAULT_PING_STALE_SEC = 25.0
# keepalive: restart only after this many seconds without a fresh heartbeat.
DEFAULT_PROBE_STALE_SEC = 120.0


def load_env(path: str) -> dict:
    d = {}
    if not os.path.exists(path):
        return d
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def write_heartbeat(path: str = DEFAULT_HEARTBEAT_PATH, now: Optional[float] = None) -> None:
    """Atomically write unix epoch of last healthy Socket Mode ping/pong."""
    ts = float(time.time() if now is None else now)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(f"{ts:.3f}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_heartbeat_age(
    path: str = DEFAULT_HEARTBEAT_PATH,
    now: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """Return (age_seconds, heartbeat_epoch). Missing/unreadable → (None, None)."""
    try:
        with open(path) as f:
            raw = f.read().strip()
        hb = float(raw.split()[0])
    except (OSError, ValueError, IndexError):
        return None, None
    t = float(time.time() if now is None else now)
    return max(0.0, t - hb), hb


def session_ping_age(smc, now: Optional[float] = None) -> Optional[float]:
    """Seconds since last Socket Mode ping/pong, or None if unknown."""
    sess = getattr(smc, "current_session", None)
    if sess is None:
        return None
    lpp = getattr(sess, "last_ping_pong_time", None)
    if lpp is None:
        return None
    t = float(time.time() if now is None else now)
    return max(0.0, t - float(lpp))


def connection_health_state(
    smc,
    *,
    ping_stale_sec: float = DEFAULT_PING_STALE_SEC,
    now: Optional[float] = None,
    connected_at: Optional[float] = None,
    warmup_sec: Optional[float] = None,
) -> str:
    """Return 'ok' | 'warmup' | 'bad'.

    warmup: connected but no ping/pong yet, still within grace after connect.
    """
    try:
        connected = bool(smc.is_connected())
    except Exception:
        return "bad"
    if not connected:
        return "bad"
    age = session_ping_age(smc, now=now)
    if age is None:
        grace = float(ping_stale_sec if warmup_sec is None else warmup_sec)
        t = float(time.time() if now is None else now)
        if connected_at is not None and (t - float(connected_at)) < grace:
            return "warmup"
        # Connected long enough with no pong → treat as bad.
        return "bad"
    if age <= float(ping_stale_sec):
        return "ok"
    return "bad"


def connection_healthy(
    smc,
    *,
    ping_stale_sec: float = DEFAULT_PING_STALE_SEC,
    now: Optional[float] = None,
    connected_at: Optional[float] = None,
    warmup_sec: Optional[float] = None,
) -> bool:
    """True when SocketModeClient reports connected and ping/pong is fresh."""
    return connection_health_state(
        smc,
        ping_stale_sec=ping_stale_sec,
        now=now,
        connected_at=connected_at,
        warmup_sec=warmup_sec,
    ) == "ok"


def auth_test_ok(
    *,
    env_path: str = DEFAULT_ENV_PATH,
    env: Optional[dict] = None,
    web_client=None,
    timeout: float = 10.0,
) -> Tuple[bool, str]:
    """Probe Slack Web API auth.test (not Socket Mode). Returns (ok, detail)."""
    cfg = env if env is not None else load_env(env_path)
    token = (cfg.get("SLACK_BOT_TOKEN") or "").strip()
    if not token:
        return False, "missing SLACK_BOT_TOKEN"
    try:
        if web_client is not None:
            web = web_client
        else:
            import ssl
            from slack_sdk.web import WebClient

            proxy = cfg.get("PROXY_URL") or None
            ca = cfg.get("CA_BUNDLE") or None
            ssl_ctx = ssl.create_default_context(
                cafile=ca if ca and os.path.exists(ca) else None
            )
            kw = {"token": token, "ssl": ssl_ctx, "timeout": timeout}
            if proxy:
                kw["proxy"] = proxy
            web = WebClient(**kw)
        r = web.auth_test()
        if r.get("ok"):
            return True, "auth.test ok"
        return False, f"auth.test not ok: {r.get('error') or r}"
    except Exception as e:
        return False, f"auth.test error: {type(e).__name__}: {e}"


def bridge_uptime_sec(
    *,
    root: str,
    proc: str = "/proc",
    now: Optional[float] = None,
) -> Optional[float]:
    """Youngest matching bridge.py process uptime in seconds, or None."""
    root_real = os.path.realpath(root)
    tnow = float(time.time() if now is None else now)
    best = None
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        base = os.path.join(proc, name)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as fh:
                parts = [p.decode() for p in fh.read().split(b"\0") if p]
            cwd = os.path.realpath(os.path.join(base, "cwd"))
        except OSError:
            continue
        if not parts or cwd != root_real:
            continue
        if not parts[0].endswith("/venv/bin/python"):
            continue
        if not any(
            a == "bridge.py"
            or a == os.path.join(root, "bridge.py")
            or a == os.path.join(root_real, "bridge.py")
            for a in parts[1:]
        ):
            continue
        try:
            # Prefer starttime from /proc/pid/stat field 22 (clock ticks).
            with open(os.path.join(base, "stat")) as fh:
                stat = fh.read()
            # comm can contain spaces/parens; split after last ')'
            after = stat.rsplit(")", 1)[-1].strip().split()
            start_ticks = int(after[19])  # field 22 → index 19 after comm
            hz = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
            with open(os.path.join(proc, "uptime")) as fh:
                uptime_sec = float(fh.read().split()[0])
            # process start age = system uptime - (start_ticks / hz)
            age = uptime_sec - (start_ticks / float(hz))
            if age < 0:
                age = 0.0
        except (OSError, ValueError, IndexError):
            try:
                age = tnow - os.stat(base).st_ctime
            except OSError:
                continue
        if best is None or age < best:
            best = age
    return best


# Exit codes for keepalive.sh
PROBE_OK = 0
PROBE_RESTART = 1
PROBE_NO_RESTART = 2


def decide_probe(
    *,
    heartbeat_age: Optional[float],
    auth_ok: bool,
    auth_detail: str,
    stale_sec: float = DEFAULT_PROBE_STALE_SEC,
    bridge_age: Optional[float] = None,
) -> Tuple[int, str]:
    """Decide keepalive action from heartbeat age + auth.test + bridge uptime.

    Returns (exit_code, reason_for_log).
    """
    if heartbeat_age is not None and heartbeat_age < float(stale_sec):
        return PROBE_OK, f"probe ok (heartbeat age={heartbeat_age:.0f}s < {stale_sec:.0f}s)"

    # No heartbeat yet, but bridge just started → wait (avoid restart storm).
    if heartbeat_age is None and bridge_age is not None and bridge_age < float(stale_sec):
        return (
            PROBE_OK,
            f"probe ok (waiting for first heartbeat; bridge age={bridge_age:.0f}s < {stale_sec:.0f}s)",
        )

    if heartbeat_age is None:
        stale_desc = "heartbeat missing"
    else:
        stale_desc = f"heartbeat stale (age={heartbeat_age:.0f}s >= {stale_sec:.0f}s)"

    if auth_ok:
        return (
            PROBE_RESTART,
            f"probe fail: {stale_desc} but {auth_detail}; restarting bridge.py",
        )
    return (
        PROBE_NO_RESTART,
        f"probe fail: {stale_desc}; {auth_detail}; not restarting",
    )


def probe_once(
    *,
    heartbeat_path: str = DEFAULT_HEARTBEAT_PATH,
    env_path: str = DEFAULT_ENV_PATH,
    stale_sec: float = DEFAULT_PROBE_STALE_SEC,
    now: Optional[float] = None,
    auth_fn=None,
    root: Optional[str] = None,
    proc: str = "/proc",
    bridge_age: Optional[float] = None,
) -> Tuple[int, str]:
    """Full external probe. auth_fn / bridge_age optional overrides for tests."""
    age, _hb = read_heartbeat_age(heartbeat_path, now=now)
    if age is not None and age < float(stale_sec):
        return decide_probe(
            heartbeat_age=age,
            auth_ok=True,
            auth_detail="skipped",
            stale_sec=stale_sec,
            bridge_age=bridge_age,
        )

    if bridge_age is None and root is not None:
        bridge_age = bridge_uptime_sec(root=root, proc=proc, now=now)

    if age is None and bridge_age is not None and bridge_age < float(stale_sec):
        return decide_probe(
            heartbeat_age=age,
            auth_ok=True,
            auth_detail="skipped",
            stale_sec=stale_sec,
            bridge_age=bridge_age,
        )

    if auth_fn is not None:
        ok, detail = auth_fn()
    else:
        ok, detail = auth_test_ok(env_path=env_path)
    return decide_probe(
        heartbeat_age=age,
        auth_ok=ok,
        auth_detail=detail,
        stale_sec=stale_sec,
        bridge_age=bridge_age,
    )


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = os.environ.get("KEEPALIVE_ROOT") or BASE
    proc = os.environ.get("KEEPALIVE_PROC") or "/proc"
    hb = os.environ.get("SOCKET_HEARTBEAT_PATH") or os.path.join(root, "socket_heartbeat")
    env_path = os.environ.get("SOCKET_ENV_PATH") or os.path.join(root, ".env")
    stale = float(os.environ.get("SOCKET_PROBE_STALE_SEC") or DEFAULT_PROBE_STALE_SEC)
    code, reason = probe_once(
        heartbeat_path=hb,
        env_path=env_path,
        stale_sec=stale,
        root=root,
        proc=proc,
    )
    print(reason)
    return code


if __name__ == "__main__":
    sys.exit(main())
