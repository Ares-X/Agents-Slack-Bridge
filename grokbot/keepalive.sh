#!/bin/bash
# Restart missing Slack bridge processes only. Never truncate logs or touch queues.
#
# Caller interval lives outside this script. The deploy loop is:
#   while true; do /workspace/slack-bridge-grokbot/keepalive.sh >> keepalive.log 2>&1; sleep 30; done
# A 5-minute agent should invoke this same script (not start python itself).
# There is no in-script sleep and no exponential backoff: the next caller tick
# (30s or 5m) is the retry. A lock timeout logs and exits 0 without starting.
#
# Double-start:
#   Exclusive flock on $ROOT/keepalive.lock around the check and the start.
#   Process identity is not pgrep -f. A match requires all of:
#     argv0 ends with /venv/bin/python
#     an argument equals the script (or $ROOT/<script>)
#     cwd is $ROOT
#   A shell whose command line only mentions bridge.py does not match.
# Tests may set KEEPALIVE_ROOT and KEEPALIVE_PROC. Unset, this is the deploy root
# and the real /proc.
set -u
ROOT=${KEEPALIVE_ROOT:-/workspace/slack-bridge-grokbot}
PROC=${KEEPALIVE_PROC:-/proc}
ts() { date '+%Y-%m-%d %H:%M:%S %z'; }
cd "$ROOT" || {
  echo "$(ts) keepalive: cannot cd $ROOT"
  exit 1
}
PY="$ROOT/venv/bin/python"
ROOT_REAL=$(readlink -f "$ROOT")

proc_matches() {
  local needle="$1"
  local d cwd a
  local -a args
  for d in "$PROC"/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    cwd=$(readlink -f "$d/cwd" 2>/dev/null || true)
    [ "$cwd" = "$ROOT_REAL" ] || continue
    args=()
    mapfile -d '' args < "$d/cmdline" || true
    [ "${#args[@]}" -ge 1 ] || continue
    case "${args[0]}" in
      */venv/bin/python) ;;
      *) continue ;;
    esac
    for a in "${args[@]:1}"; do
      if [ "$a" = "$needle" ] || [ "$a" = "$ROOT/$needle" ] || [ "$a" = "$ROOT_REAL/$needle" ]; then
        return 0
      fi
    done
  done
  return 1
}

wait_visible() {
  local needle="$1"
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if proc_matches "$needle"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

exec 9>>"$ROOT/keepalive.lock"
if ! flock -w 20 9; then
  echo "$(ts) keepalive: could not lock keepalive.lock within 20s; not starting"
  exit 0
fi

if proc_matches "bridge.py"; then
  echo "$(ts) keepalive: bridge.py already running"
else
  if [ ! -x "$PY" ]; then
    echo "$(ts) keepalive: bridge.py not running, python not executable ($PY); not starting"
  else
    echo "$(ts) keepalive: bridge.py not running, starting"
    # 9>&- so the child does not inherit the flock fd and pin the lock.
    nohup "$PY" bridge.py >> "$ROOT/bridge.log" 2>&1 < /dev/null 9>&- &
    echo "$(ts) keepalive: started bridge.py pid=$!"
    if ! wait_visible "bridge.py"; then
      echo "$(ts) keepalive: bridge.py start not visible yet pid=$!"
    fi
  fi
fi

if proc_matches "consumer/poll_consumer.py"; then
  echo "$(ts) keepalive: consumer/poll_consumer.py already running"
else
  if [ ! -x "$PY" ]; then
    echo "$(ts) keepalive: consumer/poll_consumer.py not running, python not executable ($PY); not starting"
  else
    echo "$(ts) keepalive: consumer/poll_consumer.py not running, starting"
    nohup "$PY" consumer/poll_consumer.py >> "$ROOT/consumer.log" 2>&1 < /dev/null 9>&- &
    echo "$(ts) keepalive: started consumer/poll_consumer.py pid=$!"
    if ! wait_visible "consumer/poll_consumer.py"; then
      echo "$(ts) keepalive: consumer/poll_consumer.py start not visible yet pid=$!"
    fi
  fi
fi
