#!/usr/bin/env bash
# slack-bridge-unit-watch.sh -- zero-token watchdog for the bridge systemd unit.
#
# What it does: polls the bridge process and its systemd unit; if everything
# is healthy it exits silently. If the unit is missing (e.g. the host was
# rebuilt and /etc wiped) or the service is down, it reinstalls the unit
# from the persistent template in the deployment directory and starts it --
# all inline in bash, never waking an agent, so it consumes zero tokens.
#
# Scheduling: this script has no scheduler of its own. Run it from whatever
# your host provides:
#   - a hook platform that polls silently every ~60s (silent-exit contract),
#   - or systemd timer / cron, e.g. every minute.
# Keep exactly ONE recovery mechanism enabled at a time: this watchdog, a
# periodic agent keepalive, and systemd's Restart=always must not fight each
# other. systemd Restart=always still owns process-crash recovery; this
# script only covers the unit-missing / service-dead case (VM rebuild).
#
# Configuration (environment):
#   BRIDGE_BASE   deployment dir holding bridge.py, slack-bridge.service template
#                 and .env (default: $HOME/slack-bridge)
#   PYTHON_BIN    python used for the bridge process
#                 (default: $BRIDGE_BASE/venv/bin/python)
#   SERVICE_NAME  systemd service name (default: slack-bridge)
#   HATCH_HOOK_DRY_RUN=1  (or DRY_RUN=1) report what would happen, change nothing
#
# Recovery semantic notes:
#   - A torn shutdown is recovered by the bridge's own startup path (local
#     queue files are the source of truth); this script never clears state.
#   - If the template contains a @PROXY_URL@ placeholder, it is rendered from
#     PROXY_URL in $BRIDGE_BASE/.env (or the https_proxy env var).
set -euo pipefail

BASE="${BRIDGE_BASE:-$HOME/slack-bridge}"
PYTHON_BIN="${PYTHON_BIN:-$BASE/venv/bin/python}"
SERVICE_NAME="${SERVICE_NAME:-slack-bridge}"
TPL="$BASE/slack-bridge.service"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
DRY_RUN="${HATCH_HOOK_DRY_RUN:-${DRY_RUN:-0}}"

# silent: healthy-or-recovered path. Print a one-line marker (consumed by a
# hook platform's silent contract) and exit 0. Under cron, redirect stdout.
silent() { echo "silent: $1"; exit 0; }

# 1. Bridge process alive? Nothing to do.
if pgrep -f "bridge\\.py" >/dev/null 2>&1; then
  silent "bridge process running"
fi

# 2. Unit file present and service (re)starting? Leave it to systemd.
svc_state="$(systemctl is-active "$SERVICE_NAME.service" 2>/dev/null || echo unknown)"
if [ -f "$UNIT" ] && { [ "$svc_state" = "active" ] || [ "$svc_state" = "activating" ]; }; then
  silent "bridge process missing, unit $svc_state, leaving to systemd"
fi

# 3. Need recovery: reinstall the unit from the persistent template.
if [ "$DRY_RUN" = "1" ]; then
  silent "dry run: would reinstall unit (state=$svc_state)"
fi

if grep -q '@PROXY_URL@' "$TPL" 2>/dev/null; then
  proxy=""
  if [ -f "$BASE/.env" ]; then
    proxy="$(grep -E '^PROXY_URL=' "$BASE/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
  fi
  if [ -z "$proxy" ]; then
    proxy="${https_proxy:-${HTTPS_PROXY:-}}"
  fi
  if [ -n "$proxy" ]; then
    python3 - "$TPL" "$proxy" <<'PYEOF' > /tmp/slack-bridge.service.rendered
import sys
tpl, proxy = open(sys.argv[1]).read(), sys.argv[2]
sys.stdout.write(tpl.replace("@PROXY_URL@", proxy))
PYEOF
    cp /tmp/slack-bridge.service.rendered "$UNIT"
    rm -f /tmp/slack-bridge.service.rendered
  else
    cp "$TPL" "$UNIT"
  fi
else
  cp "$TPL" "$UNIT"
fi

systemctl daemon-reload
if systemctl enable --now "$SERVICE_NAME.service" >/dev/null 2>&1; then
  sleep 5
  new_state="$(systemctl is-active "$SERVICE_NAME.service" 2>/dev/null || echo unknown)"
  if [ "$new_state" = "active" ] || [ "$new_state" = "activating" ]; then
    silent "reinstalled unit and started bridge (state=$new_state)"
  fi
fi

# 4. systemd path failed: fall back to a bare background process.
cd "$BASE" && nohup "$PYTHON_BIN" "$BASE/bridge.py" >>"$BASE/bridge.log" 2>&1 &
silent "systemd start failed, fell back to nohup"
