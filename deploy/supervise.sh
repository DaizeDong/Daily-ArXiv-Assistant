#!/usr/bin/env bash
# The scheduler for hosts that have none.
#
# A container whose PID 1 is tini has no systemd and no cron, so "install the
# timer" is not available and a bare `while true; sleep 86400` is what is left.
# Two things make that honest rather than hopeful: it writes a heartbeat on
# every tick, including ticks where the run failed, so a stuck supervisor is
# distinguishable from a failing job; and it sleeps toward a wall-clock slot
# rather than a fixed interval, so a restart does not shift the schedule and
# two restarts in a day do not skip it.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE="$REPO/deploy-state"
mkdir -p "$STATE"

SLOT_UTC="${SLOT_UTC:-05:00}"     # HH:MM, UTC
SLOT_H="${SLOT_UTC%%:*}"
SLOT_M="${SLOT_UTC##*:}"

echo "$$" > "$STATE/supervisor.pid"
echo "supervisor: started $(date -u +%FT%TZ), daily slot ${SLOT_UTC} UTC" >> "$STATE/supervisor.log"

seconds_until_slot() {
  local now target
  now=$(date -u +%s)
  target=$(date -u -d "today ${SLOT_H}:${SLOT_M}:00" +%s 2>/dev/null) || target=0
  if [ "$target" -le "$now" ]; then
    target=$(date -u -d "tomorrow ${SLOT_H}:${SLOT_M}:00" +%s)
  fi
  echo $((target - now))
}

while true; do
  # The tick is recorded before the run, so a run that hangs still shows a
  # supervisor that is alive -- and last-run.json stays stale, which is what
  # says the job, not the loop, is the thing that is stuck.
  date -u +%s > "$STATE/supervisor-heartbeat"

  if "$REPO/deploy/run_once.sh" >> "$STATE/run.log" 2>&1; then
    echo "$(date -u +%FT%TZ) run ok" >> "$STATE/supervisor.log"
  else
    echo "$(date -u +%FT%TZ) run FAILED (see run.log)" >> "$STATE/supervisor.log"
  fi

  sleep "$(seconds_until_slot)"
done
