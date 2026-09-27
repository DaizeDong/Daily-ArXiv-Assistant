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

# 0. Run today's code, not the code that was here when the host was set up. Changes are
#    made on the desktop and pushed; nothing else ever brings them to this checkout, so a
#    long-lived supervisor would otherwise keep generating with whatever it was installed
#    with. Fast-forward only: a diverged tree needs a human, and a reset would throw away
#    whatever this host holds that is not on the remote. A failed sync is recorded and the
#    run goes ahead on the code it has, because yesterday's generator still beats no day.
#    run_once.sh itself may be replaced by this merge; bash keeps reading the file it opened.
status_sync="skipped"
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  before=$(git -C "$REPO" rev-parse HEAD)
  branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
  if git -C "$REPO" fetch -q origin "$branch" \
     && git -C "$REPO" merge -q --ff-only FETCH_HEAD; then
    after=$(git -C "$REPO" rev-parse HEAD)
    if [ "$before" = "$after" ]; then
      status_sync="current"
    else
      status_sync="updated ${before:0:7}..${after:0:7}"
    fi
  else
    status_sync="failed"
    echo "run_once: could not fast-forward $branch; running the code already here" >&2
  fi
fi

# 0b. The environment this code needs, rebuilt when it is missing or out of date.
#    The container restarts and keeps the checkout but not .venv. This script used to fall back
#    to the system python3 when .venv was gone, which has none of the requirements: every run
#    from 2026-09-26 22:31 died on "No module named 'feedparser'" in 0 seconds, and the only
#    trace was this machine's own log. A stamp of requirements.txt lives INSIDE .venv, so a
#    lost venv and a changed requirements file are the same case: install. PYTHON set by the
#    operator is used as given.
status_env="ok"
if [ -z "${PYTHON:-}" ]; then
  if [ ! -x "$PY" ]; then
    rm -rf "$REPO/.venv"
    PY0="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)"
    "$PY0" -m venv "$REPO/.venv" || status_env="venv failed"
  fi
  stamp="$REPO/.venv/.requirements.sha256"
  want="$(sha256sum "$REPO/requirements.txt" | cut -c1-64)"
  if [ "$status_env" = "ok" ] && [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
    if "$PY" -m pip install -q --upgrade pip && "$PY" -m pip install -q -r "$REPO/requirements.txt"; then
      echo "$want" > "$stamp"
      status_env="reinstalled"
    else
      status_env="pip failed"
    fi
  fi
fi

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
  if [ -n "${GIT_PUSH_SSH_KEY:-}" ] || [ -n "${GIT_PUSH_TOKEN:-}" ]; then
    if "$REPO/deploy/publish.sh"; then status_publish="ok"; else status_publish="failed"; fi
  else
    status_publish="no-credential"
  fi
fi

T1=$(date +%s)
cat > "$STATE/last-run.json" <<JSON
{
  "date": "$TODAY",
  "started": "$STARTED",
  "ended": "$(date -u +%FT%TZ)",
  "seconds": $((T1 - T0)),
  "sync": "$status_sync",
  "env": "$status_env",
  "generate": "$status_generate",
  "publish": "$status_publish",
  "day_file_bytes": $( [ -f "$DAY_FILE" ] && wc -c < "$DAY_FILE" || echo 0 )
}
JSON
date -u +%s > "$STATE/heartbeat"

[ "$status_generate" = "ok" ] || exit 1
exit 0
