"""What can this host actually do? Answer before installing, not after.

The old deploy assets assumed a VPS with systemd and hardcoded
/opt/Daily-ArXiv-Assistant. The first host they were pointed at turned out to be
a container: PID 1 is tini, systemctl is absent, and there is no cron either.
An installer that assumes a scheduler on such a host completes happily and
schedules nothing, which is the worst of both -- it reports success and the job
never runs again.

So detection is a separate, testable step, and it names the scheduler it found.
"CANNOT SCHEDULE" is a real answer here, not an error: the supervised loop is
the fallback, and the caller is told which one it got.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

#: Scheduler kinds, most to least preferred.
SYSTEMD = "systemd"
CRON = "cron"
LOOP = "loop"


def _pid1_comm(proc_root: Path) -> str:
    try:
        return (proc_root / "1" / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def detect_scheduler(which=shutil.which, proc_root: Path = Path("/proc")) -> str:
    """Pick the scheduler this host can actually run.

    systemctl on PATH is not enough: a container image can carry the binary
    while PID 1 is tini, and `systemctl --user` then fails at enable time,
    after the installer has already reported success.
    """
    if which("systemctl") and _pid1_comm(proc_root) == "systemd":
        return SYSTEMD
    if which("crontab") and (which("cron") or which("crond")):
        return CRON
    return LOOP


def detect_backend(which=shutil.which, environ=os.environ) -> str:
    """Which LLM transport this host can actually carry.

    The gateway's own default is `auto`, which resolves to llmcall when the
    PACKAGE is importable -- but llmcall shells out to provider CLIs, so on a
    bare container it imports fine and then every call fails. Importability is
    not availability. This asks for the CLIs.
    """
    # Deliberately NOT "cc". It is the llmcall chain's alias for Claude Code,
    # and it is also the C compiler that POSIX requires on every Unix. Probing
    # it reported a working model transport on the first host this ran against
    # -- a container with no model CLI at all, where /usr/bin/cc is gcc 14 --
    # which would have pinned the backend to llmcall and made every call fail.
    # That is the same "present is not usable" mistake this function exists to
    # prevent, one level up. A host that really does have cc as Claude Code can
    # say so with ARXIV_ASSISTANT_LLM_BACKEND in the env file.
    if which("codexg") or which("codex") or which("claude"):
        return "auto"
    if environ.get("OPENAI_API_KEY") and environ.get("OPENAI_BASE_URL"):
        return "openai"
    return "none"


def report(which=shutil.which, proc_root: Path = Path("/proc"), environ=os.environ) -> dict:
    scheduler = detect_scheduler(which, proc_root)
    backend = detect_backend(which, environ)
    return {
        "scheduler": scheduler,
        "backend": backend,
        "pid1": _pid1_comm(proc_root),
        "python": which("python3") or which("python") or "",
        "git": which("git") or "",
        "blockers": _blockers(which, backend),
    }


def _blockers(which, backend: str) -> list[str]:
    out = []
    if not (which("python3") or which("python")):
        out.append("no python3 on PATH")
    if not which("git"):
        out.append("no git on PATH")
    if backend == "none":
        out.append("no model transport: install a provider CLI, or set "
                   "OPENAI_API_KEY and OPENAI_BASE_URL")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args()

    data = report()
    if args.json:
        print(json.dumps(data))
    else:
        for key in ("scheduler", "backend", "pid1", "python", "git"):
            print(f"{key:<10}: {data[key] or '(none)'}")
        for b in data["blockers"]:
            print(f"BLOCKER   : {b}")
    return 1 if data["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
