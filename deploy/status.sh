#!/usr/bin/env bash
# Is this deployment alive, and is it producing anything?
#
# Answers from artifacts and their ages, never from a log line saying it
# worked. Three separate ages, because they fail separately and the difference
# is the whole diagnosis: a fresh supervisor with a stale run means the job is
# stuck, a stale supervisor means the scheduler died, and a fresh run with an
# old payload means the job ran and produced nothing.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE="$REPO/deploy-state"
NOW=$(date -u +%s)

age() {  # age in human terms, or "never"
  [ -f "$1" ] || { echo "never"; return; }
  local t; t=$(stat -c %Y "$1" 2>/dev/null || echo 0)
  local d=$((NOW - t))
  if   [ $d -lt 120 ];   then echo "${d}s ago"
  elif [ $d -lt 7200 ];  then echo "$((d/60))m ago"
  else echo "$((d/3600))h ago"; fi
}

echo "repo           : $REPO"
echo "commit         : $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
echo "supervisor tick: $(age "$STATE/supervisor-heartbeat")"
echo "last run ended : $(age "$STATE/heartbeat")"

if [ -f "$STATE/supervisor.pid" ]; then
  PID=$(cat "$STATE/supervisor.pid")
  if kill -0 "$PID" 2>/dev/null; then echo "supervisor     : running (pid $PID)"
  else echo "supervisor     : DEAD (pid $PID not running)"; fi
else
  echo "supervisor     : not used (systemd or cron owns the schedule)"
fi

if [ -f "$STATE/last-run.json" ]; then
  echo "last run       :"
  sed 's/^/  /' "$STATE/last-run.json"
else
  echo "last run       : never completed"
fi

TODAY="$(date -u +%F)"
DAY="$REPO/out/web_data/hot/$TODAY.json"
if [ -s "$DAY" ]; then
  echo "today's payload: $(wc -c < "$DAY") bytes, $(age "$DAY")"
else
  echo "today's payload: MISSING ($TODAY)"
fi

NEWEST=$(ls -1 "$REPO/out/web_data/hot"/202*.json 2>/dev/null | sort | tail -1)
[ -n "$NEWEST" ] && echo "newest payload : $(basename "$NEWEST" .json)"
