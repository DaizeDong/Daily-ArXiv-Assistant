#!/usr/bin/env bash
# One-shot, idempotent install of the hotspot job on any Linux host.
#
# Replaces a VPS-only pair of systemd units that hardcoded
# /opt/Daily-ArXiv-Assistant and were never once run: pointed at a real host
# they would have failed on an import renamed months earlier. This installs on
# whatever the host turns out to be, says which scheduler it got, and refuses
# to report success until a live model call has come back.
#
#   ./deploy/install.sh --dir /opt/hotspot --env /root/hotspot.env --slot 05:00
#
# Re-running is safe: the repo is updated in place, the env file is never
# overwritten, and the scheduler is replaced rather than duplicated.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/DaizeDong/Daily-ArXiv-Assistant.git}"
TARGET="/opt/daily-arxiv-assistant"
ENV_SRC=""
SLOT_UTC="05:00"
BRANCH="main"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)    TARGET="$2"; shift 2 ;;
    --env)    ENV_SRC="$2"; shift 2 ;;
    --slot)   SLOT_UTC="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --repo)   REPO_URL="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }

say "1/7 checking out $BRANCH into $TARGET"
if [ -d "$TARGET/.git" ]; then
  git -C "$TARGET" fetch --quiet origin "$BRANCH"
  git -C "$TARGET" checkout --quiet "$BRANCH"
  git -C "$TARGET" reset --hard --quiet "origin/$BRANCH"
else
  mkdir -p "$(dirname "$TARGET")"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$TARGET"
fi
cd "$TARGET"
STATE="$TARGET/deploy-state"
mkdir -p "$STATE"

say "2/7 configuration"
ENV_FILE="$STATE/hotspot.env"
if [ -n "$ENV_SRC" ]; then
  # Copied, not symlinked: the source may live somewhere only this install can
  # read, and a dangling link would surface as an empty environment, which
  # reads exactly like "no secrets configured".
  install -m 600 "$ENV_SRC" "$ENV_FILE"
  echo "env installed from $ENV_SRC"
elif [ -f "$ENV_FILE" ]; then
  echo "env already present; left untouched"
else
  install -m 600 deploy/hotspot.env.example "$ENV_FILE"
  echo "env template written to $ENV_FILE -- fill it in before the first slot"
fi
# Sourced BEFORE detection, not after. Backend detection reads OPENAI_API_KEY
# and OPENAI_BASE_URL from the environment, so detecting first would report
# "no model transport" on a host whose credentials are sitting right there and
# refuse an install that was going to work.
set -a; . "$ENV_FILE"; set +a

say "3/7 what this host can do"
# Bootstrap with whatever python exists, only to RUN detection. Detection then
# names the interpreter the venv is built from, which is not the same thing: a
# distribution's `python3` is 3.10 on Ubuntu 22.04 while python3.12 sits beside
# it, and this code needs 3.11 for datetime.UTC.
PY0="$(command -v python3 || command -v python)"
[ -n "$PY0" ] || { echo "no python at all on PATH" >&2; exit 1; }
"$PY0" deploy/detect_target.py || {
  echo "refusing to install: the blockers above have to be fixed first" >&2
  exit 1
}
REPORT="$("$PY0" deploy/detect_target.py --json)"
field() { printf '%s' "$REPORT" | "$PY0" -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
SCHEDULER="$(field scheduler)"
BACKEND="$(field backend)"
PY_BOOT="$(field python)"
echo "interpreter   : $PY_BOOT ($(field python_version))"

say "4/7 pinning the backend"
# The gateway's `auto` resolves to llmcall whenever the PACKAGE imports, and
# llmcall then shells out to CLIs a bare host does not have. Pin what detection
# actually found instead of letting auto guess wrong.
grep -q '^ARXIV_ASSISTANT_LLM_BACKEND=' "$ENV_FILE" 2>/dev/null \
  || echo "ARXIV_ASSISTANT_LLM_BACKEND=$BACKEND" >> "$ENV_FILE"
# Re-sourced, because the line above was appended AFTER the file was read. Skip
# this and the selftest runs on whatever `auto` resolves to rather than on the
# backend this install just pinned, and passes or fails for the wrong reason.
set -a; . "$ENV_FILE"; set +a
echo "backend pinned: ${ARXIV_ASSISTANT_LLM_BACKEND:-unset}"

say "5/7 python environment"
# Built from the interpreter DETECTION chose, not from whichever python
# happened to run this script. On Ubuntu 22.04 those differ: `python3` is 3.10
# and this code needs 3.11.
[ -d .venv ] || "$PY_BOOT" -m venv .venv
PY="$TARGET/.venv/bin/python"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt

say "6/7 proving the model transport answers"
if ! "$PY" - <<'PY'
import sys
from arxiv_assistant.utils import llm_gateway
try:
    r = llm_gateway.call("Reply with exactly: OK", timeout_s=120)
except Exception as exc:
    print(f"selftest FAILED: {exc}"); sys.exit(1)
text = (r.text or "").strip()
print(f"selftest ok via {r.backend}/{r.provider or '-'}: {text[:40]!r}")
sys.exit(0 if text else 1)
PY
then
  echo "refusing to report success: the host cannot reach a model." >&2
  echo "Fix the credentials in $ENV_FILE and re-run; nothing was scheduled." >&2
  exit 1
fi

say "7/7 scheduling ($SCHEDULER)"
# The scripts are executable in the repository, so no chmod here. Doing it
# anyway changed a tracked file mode and left the checkout permanently dirty,
# which this installer survives -- it updates with `git reset --hard` -- but
# any ordinary `git pull` on the host then aborts with "local changes would be
# overwritten". A deployment that cannot be updated by hand is a deployment
# nobody will update.
case "$SCHEDULER" in
  systemd)
    sed -e "s#@TARGET@#$TARGET#g" -e "s#@SLOT@#$SLOT_UTC#g" \
        deploy/systemd/hotspot.service.in > /etc/systemd/system/hotspot.service
    sed -e "s#@SLOT@#$SLOT_UTC#g" \
        deploy/systemd/hotspot.timer.in > /etc/systemd/system/hotspot.timer
    systemctl daemon-reload
    systemctl enable --now hotspot.timer
    systemctl list-timers hotspot.timer --no-pager | head -3
    ;;
  cron)
    TMP="$(mktemp)"
    crontab -l 2>/dev/null | grep -v 'deploy/run_once.sh' > "$TMP" || true
    echo "${SLOT_UTC##*:} ${SLOT_UTC%%:*} * * * REPO=$TARGET $TARGET/deploy/run_once.sh >> $STATE/run.log 2>&1" >> "$TMP"
    crontab "$TMP"; rm -f "$TMP"
    crontab -l | grep run_once.sh
    ;;
  loop)
    if [ -f "$STATE/supervisor.pid" ] && kill -0 "$(cat "$STATE/supervisor.pid")" 2>/dev/null; then
      kill "$(cat "$STATE/supervisor.pid")" || true
      sleep 1
    fi
    REPO="$TARGET" SLOT_UTC="$SLOT_UTC" \
      nohup "$TARGET/deploy/supervise.sh" >> "$STATE/supervisor.out" 2>&1 &
    sleep 2
    echo "supervisor pid $(cat "$STATE/supervisor.pid" 2>/dev/null || echo '?')"
    echo "NOTE: this host has no systemd and no cron, so the schedule is a"
    echo "      supervised loop. It dies with the container. Check liveness"
    echo "      with deploy/status.sh, which reads artifacts, not logs."
    ;;
esac

say "done"
"$TARGET/deploy/status.sh" || true
