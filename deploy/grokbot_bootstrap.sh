#!/usr/bin/env bash
# Rebuild the Grok Bot container into a working Actions runner, from nothing.
#
# WHY THIS EXISTS, AND WHY IT IS A SCRIPT RATHER THAN A NOTE
#
# That container restarts without announcing it and comes back from its image.
# Measured across one restart: the git checkout and small untracked files under
# /workspace survived; `.venv`, `out/`, the apt-installed `ssh`, and every
# pip- and npm-installed tool did not. Nothing runs at boot -- PID 1 is tini,
# there is no systemd and no cron, and /home and /usr are back to the image, so
# even an XDG autostart entry would be gone. The machine therefore cannot heal
# itself, and the only question is how long it takes a person to heal it.
#
# Everything here is idempotent. Run it on every reattach, whether or not you
# suspect a restart; it prints what it had to rebuild, which is also the only
# reliable evidence that a restart happened at all.
#
# SECRETS
#
# Nothing sensitive is stored by this script. The eight workflow secrets are
# injected by GitHub into each job's environment and are gone when the job
# ends, which is why the jobs were moved onto a runner rather than ported to
# scripts with an env file: an env file on this machine is at rest, and this
# machine is shared with every other Bot on the account.
#
# The registration token is read from a file, used once, and shredded. The
# runner's own identity (.credentials_rsaparams) does stay on disk while the
# runner runs, because the runner reads it on every poll; that is inherent, not
# a choice. It is mode 600 and can be revoked by removing the runner in the
# repository settings, which is the repair if this machine is ever suspect.
#
#   ./deploy/grokbot_bootstrap.sh --token-file /path/to/token.env
#
# The token file is `RUNNER_TOKEN=...`, one line, written with the terminal's
# echo off -- see grokbot_cdp.write_env_file. A registration token is valid for
# one hour and is useless once consumed.
set -uo pipefail

RUNNER_DIR="${RUNNER_DIR:-/workspace/actions-runner}"
RUNNER_VERSION="${RUNNER_VERSION:-2.337.0}"
PYTHON_TOOL_VERSION="${PYTHON_TOOL_VERSION:-3.12.14}"
PYTHON_TOOL_TAG="${PYTHON_TOOL_TAG:-3.12.14-31661455385}"
PYTHON_TOOL_UBUNTU="${PYTHON_TOOL_UBUNTU:-24.04}"
REPO_URL="${REPO_URL:-https://github.com/DaizeDong/Daily-ArXiv-Assistant}"
RUNNER_NAME="${RUNNER_NAME:-grokbot-container}"
RUNNER_LABELS="${RUNNER_LABELS:-grokbot,linux,container}"
TOKEN_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --dir)        RUNNER_DIR="$2"; shift 2 ;;
    -h|--help)    sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n== %s\n' "$*"; }
rebuilt=""
note() { rebuilt="${rebuilt}${1}, "; }

TOOLS="$RUNNER_DIR/_work/_tool"

say "1/5 system packages"
# `ssh` is the one that bites: the deploy key survives a restart and the binary
# that uses it does not, so git reports "correct access rights" and a publisher
# that generates perfectly stops publishing. apt's package lists are also from
# the image and are usually too old to resolve anything, hence the update.
if command -v ssh >/dev/null 2>&1; then
  echo "ssh present"
else
  note "openssh-client"
  sudo apt-get update -qq >/dev/null 2>&1
  sudo apt-get install -y -qq openssh-client >/dev/null 2>&1
  command -v ssh >/dev/null 2>&1 || { echo "could not install ssh" >&2; exit 1; }
  echo "ssh installed"
fi

say "2/5 runner binaries"
if [ -x "$RUNNER_DIR/config.sh" ]; then
  echo "runner $RUNNER_VERSION already extracted"
else
  note "runner"
  mkdir -p "$RUNNER_DIR"
  URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  curl -sSL -o "$RUNNER_DIR/rt.tar.gz" "$URL" || { echo "download failed" >&2; exit 1; }
  tar xzf "$RUNNER_DIR/rt.tar.gz" -C "$RUNNER_DIR"
  rm -f "$RUNNER_DIR/rt.tar.gz"
  sudo "$RUNNER_DIR/bin/installdependencies.sh" >/dev/null 2>&1 \
    || { echo "installdependencies failed" >&2; exit 1; }
  echo "runner $RUNNER_VERSION extracted, dependencies installed"
fi

say "3/5 python in the runner tool cache"
# setup-python cannot serve this host: actions/python-versions publishes
# Ubuntu builds only, and on Debian 13 the action fails with "The version
# '3.12' with architecture 'x64' was not found for debian 13". An Ubuntu 24.04
# build does run here -- glibc is 2.41 against that build's 2.39 -- so the
# answer is to put it in the tool cache, where setup-python looks first. That
# keeps the workflows identical on both runners, which is the point.
ARCHP="$TOOLS/Python/$PYTHON_TOOL_VERSION/x64"
if [ -f "$TOOLS/Python/$PYTHON_TOOL_VERSION/x64.complete" ]; then
  echo "python $PYTHON_TOOL_VERSION already in the tool cache"
else
  note "python tool cache"
  mkdir -p "$TOOLS"
  WORK="$(mktemp -d)"
  URL="https://github.com/actions/python-versions/releases/download/${PYTHON_TOOL_TAG}/python-${PYTHON_TOOL_VERSION}-linux-${PYTHON_TOOL_UBUNTU}-x64.tar.gz"
  curl -sSL -o "$WORK/py.tar.gz" "$URL" || { echo "download failed" >&2; exit 1; }
  tar xzf "$WORK/py.tar.gz" -C "$WORK"
  # LD_LIBRARY_PATH is not optional here. The build is --enable-shared, so the
  # interpreter cannot start without libpython3.12.so.1.0 on the loader path;
  # setup.sh runs the interpreter to upgrade pip, dies there, and never writes
  # the .complete marker -- leaving a tool cache that looks installed and that
  # setup-python silently ignores. In a real job the action exports this
  # itself; here nothing does.
  ( cd "$WORK" && AGENT_TOOLSDIRECTORY="$TOOLS" \
      LD_LIBRARY_PATH="$ARCHP/lib" bash ./setup.sh >/dev/null 2>&1 ) \
    || { echo "python tool cache setup failed" >&2; rm -rf "$WORK"; exit 1; }
  rm -rf "$WORK"
  [ -f "$TOOLS/Python/$PYTHON_TOOL_VERSION/x64.complete" ] \
    || { echo "setup.sh reported success but wrote no .complete marker" >&2; exit 1; }
  echo "python $PYTHON_TOOL_VERSION installed into the tool cache"
fi
LD_LIBRARY_PATH="$ARCHP/lib" "$ARCHP/bin/python3" -V >/dev/null 2>&1 \
  || { echo "the cached interpreter does not run on this host" >&2; exit 1; }

say "4/5 runner registration"
if [ -f "$RUNNER_DIR/.runner" ] && [ -f "$RUNNER_DIR/.credentials" ]; then
  echo "already registered as $(sed -n 's/.*"agentName": *"\([^"]*\)".*/\1/p' "$RUNNER_DIR/.runner")"
elif [ -n "$TOKEN_FILE" ] && [ -f "$TOKEN_FILE" ]; then
  note "registration"
  set -a; . "$TOKEN_FILE"; set +a
  ( cd "$RUNNER_DIR" && ./config.sh --unattended --url "$REPO_URL" \
      --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" --labels "$RUNNER_LABELS" \
      --work _work --replace ) || { echo "registration failed" >&2; exit 1; }
  unset RUNNER_TOKEN
  shred -u "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE"
  chmod 600 "$RUNNER_DIR/.credentials" "$RUNNER_DIR/.credentials_rsaparams" "$RUNNER_DIR/.runner"
  echo "registered as $RUNNER_NAME, token file destroyed"
else
  echo "not registered, and no --token-file given." >&2
  echo "Mint one with: gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token -q .token" >&2
  echo "and deliver it with echo off; see the header of this script." >&2
  exit 1
fi

say "5/5 the listener"
# `pgrep -f Runner.Listener` and not a pid file: the pid file outlives the
# process across a restart, which is exactly the case this has to detect.
if pgrep -f "Runner.Listener" >/dev/null 2>&1; then
  echo "listener already running (pid $(pgrep -f Runner.Listener | head -1))"
else
  note "listener"
  ( cd "$RUNNER_DIR" && nohup ./run.sh > "$RUNNER_DIR/runner.out" 2>&1 & )
  sleep 12
  pgrep -f "Runner.Listener" >/dev/null 2>&1 \
    || { echo "listener did not start; see $RUNNER_DIR/runner.out" >&2
         tail -5 "$RUNNER_DIR/runner.out" 2>/dev/null; exit 1; }
  echo "listener started (pid $(pgrep -f Runner.Listener | head -1))"
fi

grep -q "Listening for Jobs" "$RUNNER_DIR/runner.out" 2>/dev/null \
  && echo "runner reports: Listening for Jobs" \
  || echo "note: runner.out does not say 'Listening for Jobs' yet"

printf '\n== done\n'
echo "uptime        : $(uptime -p 2>/dev/null || uptime)"
if [ -n "$rebuilt" ]; then
  echo "rebuilt       : ${rebuilt%, }"
  echo "                (anything rebuilt means this host restarted under us)"
else
  echo "rebuilt       : nothing; the host has not restarted since the last run"
fi
