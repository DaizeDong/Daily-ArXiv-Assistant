# The `grokbot` runner

The jobs in this repository target `arxiv-fleet`, a label carried by every
runner that can serve them. `grokbot` is one of those runners, and the one
most likely to need putting back. This is what that machine is, what is odd
about it, and how to rebuild it.

Because the fleet is shared, this machine being down is not an outage: the
Windows runners take the queue. It does mean a job may run here or there, so
anything true of one runner has to be true of both, which
`runner_selftest.yml` is there to check.

## What the machine is

It is the cloud computer behind a Grok Bot (xAI's computer-use agent), reached
over the Chrome DevTools Protocol rather than through an API, because there is
no API: the only documented way to use it is to message the Bot. The library
that does the reaching is [grokbot-cdp](https://github.com/DaizeDong/grokbot-cdp),
which also documents the protocol quirks. Everything below was measured on it.

| | |
| --- | --- |
| what it is | a container, not a VM: PID 1 is `tini` |
| scheduler | none. No systemd, no cron, nothing at boot |
| OS | Debian 13 (trixie), x86_64, glibc 2.41 |
| `sudo` | passwordless |
| shared with | every other Bot on the account, files and browser sessions included |
| runner root | `/workspace/actions-runner` |
| repo checkout | `/workspace/app` |

## The one thing to know: it restarts, and says nothing

The container restarts without warning and comes back from its image. Measured
across one restart:

- **Survived**: the git checkout under `/workspace`, and small untracked files
  beside it.
- **Gone**: `.venv`, `out/`, the apt-installed `ssh`, every pip- and
  npm-installed tool, and the runner's own process.

`.venv` is not in `.gitignore`, so "it respects .gitignore" is not the rule,
and one observation is not enough to state what the rule is. Assume nothing
untracked survives.

Nothing announces any of this. The runner's log stops rather than erroring,
which reads like a quiet night; the Actions queue fills with jobs that are
merely *queued*, which is what a busy machine also looks like. **`uptime` is
the only thing that says a restart happened.** Check it when you reattach.

## Putting it back

```
./deploy/grokbot_bootstrap.sh --token-file /path/to/token.env
```

Idempotent: run it on every reattach whether or not you suspect anything. It
prints what it had to rebuild, and `rebuilt: nothing` is the statement that the
host has not restarted. It does not touch a listener that is already running.

The token file holds one line, `RUNNER_TOKEN=...`, from
`gh api -X POST repos/OWNER/REPO/actions/runners/registration-token -q .token`.
A registration token lasts an hour and is useless once consumed. Deliver it
with the terminal's echo off (`grokbot_cdp.write_env_file`), because anything
typed with echo on is in every screenshot taken during or after the run. The
script shreds the file after using it. If the runner is already registered the
token is not needed at all.

## Two things that are not obvious, and cost a run each

**`setup-python` cannot serve this host.** `actions/python-versions` publishes
Ubuntu builds only, so the action fails with `The version '3.12' with
architecture 'x64' was not found for debian 13`. An Ubuntu 24.04 build does run
here, so the bootstrap puts one in the runner's tool cache, where the action
looks before downloading. That keeps the workflow files identical on every
runner, which is the point.

**That build is `--enable-shared`.** Its interpreter will not start without
`libpython3.12.so.1.0` on the loader path. In a real job the action exports
`LD_LIBRARY_PATH` itself; during installation nothing does, so the vendor's own
`setup.sh` dies upgrading pip and never writes the `.complete` marker, leaving
a tool cache that looks installed and that `setup-python` silently ignores. The
bootstrap sets it, then checks the marker exists rather than trusting the exit
code.

## Secrets

**Nothing sensitive is stored on the machine by this deployment**, which is the
reason the jobs run on a runner instead of being ported to scripts with an env
file. GitHub injects each workflow's secrets into the job's process environment
and they are gone when the job ends; an env file would be at rest, on a machine
shared with every other Bot on the account, and those Bots browse the web.

Two exceptions, both inherent rather than chosen:

- The runner's own identity (`.credentials_rsaparams`) is on disk while the
  runner runs, because it is read on every poll. Mode 600. The repair, if the
  machine is ever suspect, is to remove the runner in the repository settings,
  which revokes it.
- While a job runs, that job's secrets are in its environment and readable by
  anything else on the machine. This is true of every self-hosted runner and is
  the exposure that was accepted when the jobs were moved here.

## Pinning a job away from here

Change its `runs-on` to `[ self-hosted, windows ]`. Nothing else in these
workflows is machine-specific: every `run:` step declares `shell: bash`, and
`tests/test_workflows_are_portable.py` keeps it that way.

To take this machine out of the fleet entirely without touching any workflow,
remove the `arxiv-fleet` label from the runner in the repository settings.
