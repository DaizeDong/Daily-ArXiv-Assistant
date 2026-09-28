#!/usr/bin/env bash
# Copy one hotspot run's evidence into a private companion repository.
#
# run_once.sh calls this last, after last-run.json and the heartbeat are written. The files it
# copies all live in deploy-state/, which is outside version control and is the only record of
# what a run did; a host rebuild takes it with everything else. A private companion repository
# gives that record history and a diff, and keeps it out of this one, which is public.
#
# WHAT LANDS WHERE, in the companion:
#   data/hotspot/<UTC yyyy-mm-dd>-<HHMMSS>/
#       last-run.json         the run's own summary, as run_once.sh wrote it
#       run.log               this run's part of deploy-state/run.log (from its start marker)
#       supervisor.log.tail   the last 100 lines of deploy-state/supervisor.log
#
# CONFIGURATION, in deploy-state/hotspot.env like every other setting:
#   ARXIV_COMPANION_REMOTE    the companion's clone url. Unset: print a notice and do nothing.
#   ARXIV_COMPANION_SSH_KEY   a write deploy key for it; required when the remote is ssh.
#   ARXIV_COMPANION_DIR       where the companion checkout lives (default deploy-state/companion)
#   ARXIV_COMPANION_BRANCH    the branch to push (default main)
#
# IT NEVER FAILS THE RUN. The run has already succeeded or failed on its own merits, and
# run_once.sh's exit code is decided by generation alone. Every problem here is a ::warning::
# line in run.log and an exit 0.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE="${STATE:-$REPO/deploy-state}"
ENV_FILE="${ENV_FILE:-$STATE/hotspot.env}"
if [ -z "${ARXIV_COMPANION_REMOTE:-}" ] && [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

REMOTE="${ARXIV_COMPANION_REMOTE:-}"
KEY="${ARXIV_COMPANION_SSH_KEY:-}"
COMPANION="${ARXIV_COMPANION_DIR:-$STATE/companion}"
BRANCH="${ARXIV_COMPANION_BRANCH:-main}"
AUTHOR_NAME="${GIT_AUTHOR_NAME_OVERRIDE:-github-actions[bot]}"
AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL_OVERRIDE:-github-actions[bot]@users.noreply.github.com}"
MARKER="=== run_once start"

warn() { echo "::warning::archive_run: $*"; }

if [ -z "$REMOTE" ]; then
  echo "archive_run: notice: ARXIV_COMPANION_REMOTE is not set; this run is not archived"
  exit 0
fi

# ssh remotes need the deploy key; a local path or an https url with its own credential does not.
ssh_ready() {
  case "$REMOTE" in
    git@*|ssh://*) ;;
    *) return 0 ;;
  esac
  [ -n "$KEY" ] || { warn "ARXIV_COMPANION_SSH_KEY is not set and the remote is ssh"; return 1; }
  [ -f "$KEY" ] || { warn "no deploy key at $KEY; the companion cannot be reached"; return 1; }
  command -v ssh >/dev/null 2>&1 || { warn "no ssh on PATH (lost in a container restart?)"; return 1; }
  export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30"
}

ensure_companion() {
  [ -d "$COMPANION/.git" ] && return 0
  ssh_ready || return 1
  mkdir -p "$(dirname "$COMPANION")"
  if ! git clone -q "$REMOTE" "$COMPANION" > "$STATE/archive.clone.log" 2>&1; then
    warn "could not clone the companion into $COMPANION"
    sed 's/^/    /' "$STATE/archive.clone.log" | tail -4
    return 1
  fi
  # A new companion is empty; point its unborn HEAD at the branch this pushes.
  git -C "$COMPANION" rev-parse -q --verify HEAD >/dev/null 2>&1 \
    || git -C "$COMPANION" symbolic-ref HEAD "refs/heads/$BRANCH"
  echo "archive_run: cloned the companion into $COMPANION"
}

collect() {
  local dest="$1" start
  mkdir -p "$dest" || { warn "cannot create $dest"; return 1; }
  if [ -f "$STATE/last-run.json" ]; then
    cp -f "$STATE/last-run.json" "$dest/last-run.json"
  else
    warn "no last-run.json"
  fi
  if [ -f "$STATE/run.log" ]; then
    # From this run's start marker; without one (a log older than the marker), the last 400 lines.
    start="$(grep -n -F "$MARKER" "$STATE/run.log" | tail -1 | cut -d: -f1)"
    if [ -n "$start" ]; then
      tail -n "+$start" "$STATE/run.log" | tail -n 5000 > "$dest/run.log"
    else
      tail -n 400 "$STATE/run.log" > "$dest/run.log"
    fi
  fi
  [ -f "$STATE/supervisor.log" ] && tail -n 100 "$STATE/supervisor.log" > "$dest/supervisor.log.tail"
  echo "archive_run: copied the run's evidence into ${dest#"$COMPANION"/}"
}

publish() {
  local i
  git -C "$COMPANION" add -A data || { warn "git add failed in the companion"; return 1; }
  if git -C "$COMPANION" diff --cached --quiet; then
    echo "archive_run: nothing new to commit"
  else
    git -C "$COMPANION" -c user.name="$AUTHOR_NAME" -c user.email="$AUTHOR_EMAIL" \
      commit -q -m "hotspot: archive run at $(date -u +%FT%TZ)" \
      || { warn "commit failed in the companion"; return 1; }
  fi
  ssh_ready || { warn "committed locally; the push waits for the key"; return 1; }
  for i in 1 2 3; do
    if git -C "$COMPANION" ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
      if ! git -C "$COMPANION" -c user.name="$AUTHOR_NAME" -c user.email="$AUTHOR_EMAIL" \
             pull -q --rebase origin "$BRANCH" > "$STATE/archive.pull.log" 2>&1; then
        git -C "$COMPANION" rebase --abort >/dev/null 2>&1
        warn "pull --rebase failed (attempt $i)"
        sed 's/^/    /' "$STATE/archive.pull.log" | tail -4
        sleep $((i * 5))
        continue
      fi
    fi
    if git -C "$COMPANION" push -q origin "HEAD:refs/heads/$BRANCH" > "$STATE/archive.push.log" 2>&1; then
      echo "archive_run: pushed to the companion ($BRANCH)"
      return 0
    fi
    warn "push failed (attempt $i)"
    sed 's/^/    /' "$STATE/archive.push.log" | tail -4
    sleep $((i * 5))
  done
  warn "gave up pushing after 3 attempts; the commit stays in the companion for the next run"
  return 1
}

main() {
  mkdir -p "$STATE"
  ensure_companion || return 1
  collect "$COMPANION/data/hotspot/$(date -u +%F-%H%M%S)" || return 1
  publish || return 1
}

main || warn "this run was not archived (see above); the run itself is unaffected"
exit 0
