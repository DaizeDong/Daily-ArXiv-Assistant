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


#: The oldest interpreter this code actually runs on. `from datetime import
#: UTC` appears in eight modules and arrived in 3.11; CI runs 3.12. A host with
#: 3.10 imports fine right up to the first of those modules, which is not
#: necessarily on the installer's selftest path -- so without this check the
#: install reports success and the nightly run is the thing that fails.
MIN_PYTHON = (3, 11)

#: Interpreters to try, best first. A distribution's `python3` is often older
#: than a version-suffixed one installed alongside it.
PYTHON_NAMES = ("python3.13", "python3.12", "python3.11", "python3", "python")


def _version_of(executable: str, runner=None) -> tuple[int, int] | None:
    """Ask the interpreter its version. Asking is the point: the NAME lies.

    python3.12 can be a symlink to something else, and `python3` on one host is
    3.10 while on the next it is 3.13.
    """
    import subprocess

    run = runner or subprocess.run
    try:
        out = run([executable, "-c",
                   "import sys;print('%d.%d' % sys.version_info[:2])"],
                  capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 - an interpreter that will not run is absent
        return None
    if getattr(out, "returncode", 1) != 0:
        return None
    try:
        major, minor = (out.stdout or "").strip().split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return None


def find_python(which=shutil.which, runner=None) -> tuple[str, tuple[int, int]] | None:
    """The best interpreter on this host that is new enough, or None."""
    best: tuple[str, tuple[int, int]] | None = None
    for name in PYTHON_NAMES:
        path = which(name)
        if not path:
            continue
        version = _version_of(path, runner)
        if version is None or version < MIN_PYTHON:
            continue
        if best is None or version > best[1]:
            best = (path, version)
    return best


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
    # Either credential shape counts. A gateway does not have to accept the
    # SDK's Authorization: Bearer -- the one this was built against is Azure
    # API Management, which wants Ocp-Apim-Subscription-Key and has no API key
    # at all. Requiring OPENAI_API_KEY would report "no model transport" on a
    # host that reaches a model perfectly well, and block its own install.
    has_auth = bool(environ.get("OPENAI_API_KEY") or environ.get("OPENAI_EXTRA_HEADERS"))
    if has_auth and environ.get("OPENAI_BASE_URL"):
        return "openai"
    return "none"


def report(which=shutil.which, proc_root: Path = Path("/proc"), environ=os.environ,
           runner=None) -> dict:
    scheduler = detect_scheduler(which, proc_root)
    backend = detect_backend(which, environ)
    python = find_python(which, runner)
    return {
        "scheduler": scheduler,
        "backend": backend,
        "pid1": _pid1_comm(proc_root),
        "python": python[0] if python else "",
        "python_version": "%d.%d" % python[1] if python else "",
        "git": which("git") or "",
        "blockers": _blockers(which, backend, python),
    }


def _blockers(which, backend: str, python=None) -> list[str]:
    out = []
    if python is None:
        want = "%d.%d" % MIN_PYTHON
        if which("python3") or which("python"):
            out.append("python is present but older than %s; this code uses "
                       "datetime.UTC, which arrived in %s" % (want, want))
        else:
            out.append("no python on PATH (need %s or newer)" % want)
    if not which("git"):
        out.append("no git on PATH")
    if backend == "none":
        out.append("no model transport: install a provider CLI, or set "
                   "OPENAI_BASE_URL plus either OPENAI_API_KEY or "
                   "OPENAI_EXTRA_HEADERS")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args()

    data = report()
    if args.json:
        print(json.dumps(data))
    else:
        for key in ("scheduler", "backend", "pid1", "python", "python_version", "git"):
            print(f"{key:<10}: {data[key] or '(none)'}")
        for b in data["blockers"]:
            print(f"BLOCKER   : {b}")
    return 1 if data["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
