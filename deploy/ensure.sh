#!/usr/bin/env bash
# Put the schedule back if it is gone, and say which case it was.
#
# On a host with systemd this is unnecessary: the timer is restored at boot.
# On a host without one -- the Grok Bot container, whose PID 1 is tini -- the
# supervisor is a plain background process, and a restart takes it with no
# trace beyond a pid file pointing at nothing. That happened: the container
# came back with `up 9 min`, an empty `out/`, and a supervisor.pid whose
# process was gone. Nothing failed, nothing logged, and the schedule was
# simply no longer there.
#
# So this is the missing init, reduced to the one thing an init would do, and
# it is idempotent: run it on every connection, whether or not you suspect
# anything. It prints one of three verdicts and exits 0 only when the schedule
# is actually running afterwards.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"
STATE="$REPO/deploy-state"
mkdir -p "$STATE"

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files hotspot.timer >/dev/null 2>&1; then
  systemctl is-active --quiet hotspot.timer && { echo "ensure: systemd timer active, nothing to do"; exit 0; }
  echo "ensure: systemd owns the schedule but hotspot.timer is not active"
  exit 1
fi

if [ -f "$STATE/supervisor.pid" ]; then
  PID=$(cat "$STATE/supervisor.pid" 2>/dev/null || echo "")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "ensure: supervisor already running (pid $PID)"
    exit 0
  fi
  echo "ensure: supervisor.pid holds ${PID:-nothing} but no such process -- the host restarted under it"
else
  echo "ensure: no supervisor has ever been started here"
fi

# out/ is untracked, so a restart takes the day's work with it unless it was
# already published. Say so rather than letting the next run look like a
# normal first run of the day.
if [ ! -d "$REPO/out/web_data/hot" ]; then
  echo "ensure: out/web_data/hot is gone; anything generated and not yet published is lost"
fi

nohup "$REPO/deploy/supervise.sh" > "$STATE/supervisor.out" 2>&1 &
NEW=$!
sleep 2
if kill -0 "$NEW" 2>/dev/null; then
  echo "ensure: supervisor started (pid $NEW)"
  exit 0
fi
echo "ensure: supervisor exited immediately; see $STATE/supervisor.out"
tail -5 "$STATE/supervisor.out" 2>/dev/null
exit 1
