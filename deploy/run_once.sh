#!/usr/bin/env bash
# One hotspot cycle, leaving evidence behind.
#
# Every stage records what it did into deploy-state/last-run.json and touches
# deploy-state/heartbeat. That is deliberate: this job has no exit code anyone
# will ever see -- a timer swallows it, and the loop supervisor outlives it --
# so "is it alive and working" has to be answerable from artifacts alone. The
# last outage here lasted four days precisely because the only evidence was a
# run that stopped without failing.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

STATE="$REPO/deploy-state"
mkdir -p "$STATE"

ENV_FILE="${ENV_FILE:-$STATE/hotspot.env}"
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

TODAY="$(date -u +%F)"
STARTED="$(date -u +%FT%TZ)"
T0=$(date +%s)

PY="${PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

status_generate="skipped"
status_publish="skipped"

# 1. Generate. --force so a partial earlier attempt does not make this a no-op.
if "$PY" scripts/generate_daily_hotspots.py \
      --output-root out --mode "${HOTSPOT_MODE:-auto}" --date "$TODAY"; then
  status_generate="ok"
else
  status_generate="failed"
fi

# 2. Publish, only if generation produced today's payload. Pushing an unchanged
#    tree would still make a commit look like progress.
DAY_FILE="out/web_data/hot/$TODAY.json"
if [ "$status_generate" = "ok" ] && [ -s "$DAY_FILE" ]; then
  if [ -n "${GIT_PUSH_TOKEN:-}" ]; then
    if "$REPO/deploy/publish.sh"; then status_publish="ok"; else status_publish="failed"; fi
  else
    status_publish="no-token"
  fi
fi

T1=$(date +%s)
cat > "$STATE/last-run.json" <<JSON
{
  "date": "$TODAY",
  "started": "$STARTED",
  "ended": "$(date -u +%FT%TZ)",
  "seconds": $((T1 - T0)),
  "generate": "$status_generate",
  "publish": "$status_publish",
  "day_file_bytes": $( [ -f "$DAY_FILE" ] && wc -c < "$DAY_FILE" || echo 0 )
}
JSON
date -u +%s > "$STATE/heartbeat"

[ "$status_generate" = "ok" ] || exit 1
exit 0
