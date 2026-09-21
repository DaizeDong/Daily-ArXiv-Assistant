#!/usr/bin/env bash
# Push generated hotspot data to the auto_update branch.
#
# Reconciles the same way the Pages workflow does: reset to origin, merge only
# the fields this host owns, commit, push, and on a lost race read the branch
# again rather than insisting on the copy we started from. Never --force: the
# daily pipeline writes here too and its output is not ours to discard.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO"

: "${GIT_PUSH_TOKEN:?GIT_PUSH_TOKEN is required to publish}"
SLUG="${GIT_REPO_SLUG:-DaizeDong/Daily-ArXiv-Assistant}"
AUTHOR_NAME="${GIT_AUTHOR_NAME_OVERRIDE:-github-actions[bot]}"
AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL_OVERRIDE:-github-actions[bot]@users.noreply.github.com}"

WORK="$REPO/deploy-state/auto_update"
REMOTE="https://x-access-token:${GIT_PUSH_TOKEN}@github.com/${SLUG}.git"

if [ ! -d "$WORK/.git" ]; then
  rm -rf "$WORK"
  git clone --branch auto_update --single-branch "$REMOTE" "$WORK" >/dev/null 2>&1
fi

for attempt in 1 2 3; do
  git -C "$WORK" remote set-url origin "$REMOTE"
  git -C "$WORK" fetch origin auto_update --quiet
  git -C "$WORK" reset --hard origin/auto_update --quiet

  mkdir -p "$WORK/out/web_data/hot"
  # Whole files here, not a field merge: this host GENERATED the day, so it owns
  # the content. The Pages build merges only _zh because it owns only that.
  cp -f out/web_data/hot/*.json "$WORK/out/web_data/hot/" 2>/dev/null || true

  git -C "$WORK" add -f out/web_data/hot/
  if git -C "$WORK" diff --cached --quiet; then
    echo "publish: nothing new"
    exit 0
  fi

  git -C "$WORK" -c "user.name=$AUTHOR_NAME" -c "user.email=$AUTHOR_EMAIL" \
      commit -q -m "[skip ci] Hotspots from $(hostname): $(date -u +'%F %T')"
  if git -C "$WORK" push -q origin HEAD:auto_update; then
    echo "publish: pushed on attempt $attempt"
    exit 0
  fi
  echo "publish: lost a race on attempt $attempt; re-reading the branch"
  sleep 5
done

echo "publish: could not push after 3 attempts"
exit 1
