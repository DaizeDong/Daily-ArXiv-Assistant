"""deploy/archive_run.sh copies each hotspot run's evidence into a private companion repository.

Pinned here: run_once.sh calls it after last-run.json and the heartbeat exist and before its own
verdict, and cannot have its exit code changed by it; with no companion configured it says so and
exits 0; with an ssh remote and no key it warns and exits 0; and against a reachable companion
(a local bare repository standing in for the real one) it clones, commits, pushes, and on the
next run rebases onto whatever arrived in between.

The executed tests run the real run_once.sh in a throwaway REPO whose PYTHON is a stub, so no
model, network or venv is involved. Git's global and system config are replaced with empty ones
so the developer machine's hooks and identity stay out of the throwaway commits.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
ARCHIVE = DEPLOY / "archive_run.sh"
RUN_ONCE = DEPLOY / "run_once.sh"

BASH = shutil.which("bash")
GIT = shutil.which("git")


class ArchiveWiringTests(unittest.TestCase):
    def test_the_archive_runs_after_the_evidence_and_before_the_verdict(self):
        text = RUN_ONCE.read_text(encoding="utf-8")
        body = text.split("T1=$(date +%s)", 1)[1]
        call = body.index("deploy/archive_run.sh")
        self.assertLess(body.index("last-run.json"), call)
        self.assertLess(body.index('> "$STATE/heartbeat"'), call)
        self.assertLess(call, body.index('[ "$status_generate" = "ok" ] || exit 1'),
                        "the archive runs after the verdict, so a failed run skips it")

    def test_the_archive_cannot_change_the_exit_code(self):
        text = RUN_ONCE.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "archive_run.sh" in line and "bash " in line:
                self.assertTrue(line.rstrip().endswith("|| true"),
                                "an archive call can fail run_once: %r" % line)

    def test_run_once_marks_where_its_log_starts(self):
        text = RUN_ONCE.read_text(encoding="utf-8")
        marker = 'echo "=== run_once start'
        self.assertIn(marker, text)
        self.assertIn("=== run_once start", ARCHIVE.read_text(encoding="utf-8"))
        self.assertLess(text.index(marker), text.index("generate_daily_hotspots.py"))

    def test_the_archive_script_always_exits_zero(self):
        lines = [l for l in ARCHIVE.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(lines[-1].strip(), "exit 0")
        self.assertNotIn("exit 1", "\n".join(lines))

    def test_the_remote_is_configuration_not_code(self):
        # This repository is public; which private repository holds the runs is a host setting.
        text = ARCHIVE.read_text(encoding="utf-8")
        self.assertIn('REMOTE="${ARXIV_COMPANION_REMOTE:-}"', text)
        self.assertNotIn("github.com:", text)
        self.assertNotIn("github.com/", text)
        self.assertIn("ARXIV_COMPANION_REMOTE=", (DEPLOY / "hotspot.env.example").read_text(encoding="utf-8"))


@unittest.skipIf(BASH is None or GIT is None, "needs bash and git")
class ArchiveExecutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        gcfg = self.tmp / "gitconfig"
        gcfg.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and not k.startswith("ARXIV_COMPANION_")}
        env.update({"GIT_CONFIG_GLOBAL": str(gcfg), "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": str(self.tmp), "GIT_CEILING_DIRECTORIES": str(self.tmp)})
        self.env = env

        # A throwaway REPO: the real deploy scripts, a stub interpreter, no git checkout.
        self.repo = self.tmp / "app"
        (self.repo / "deploy").mkdir(parents=True)
        shutil.copy(RUN_ONCE, self.repo / "deploy" / "run_once.sh")
        shutil.copy(ARCHIVE, self.repo / "deploy" / "archive_run.sh")
        (self.repo / "requirements.txt").write_text("", encoding="utf-8")
        self.state = self.repo / "deploy-state"
        self.state.mkdir()
        (self.state / "run.log").write_text(
            "=== run_once start 2026-01-01T00:00:00Z ===\nyesterday\n", encoding="utf-8")
        (self.state / "supervisor.log").write_text("supervisor: started\n", encoding="utf-8")
        self.env.update({"REPO": self.repo.as_posix(),
                         "ENV_FILE": (self.tmp / "absent.env").as_posix()})

    def tearDown(self):
        self._tmp.cleanup()

    def stub_python(self, rc):
        stub = self.tmp / f"python-{rc}"
        stub.write_text(f"#!/usr/bin/env bash\necho generating\nexit {rc}\n", encoding="utf-8")
        stub.chmod(0o755)
        self.env["PYTHON"] = stub.as_posix()

    def run_once(self):
        # As the supervisor does it: stdout appended to run.log.
        with open(self.state / "run.log", "ab") as log:
            p = subprocess.run([BASH, (self.repo / "deploy" / "run_once.sh").as_posix()],
                               env=self.env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        return p.returncode, (self.state / "run.log").read_text(encoding="utf-8")

    def git(self, *args, cwd):
        return subprocess.run([GIT, *args], cwd=str(cwd), env=self.env,
                              capture_output=True, text=True)

    def test_unconfigured_is_a_notice_and_exit_codes_are_preserved(self):
        self.stub_python(1)
        rc, log = self.run_once()
        self.assertEqual(rc, 1, "a failed generation must still exit 1 with the archive attached")
        self.assertIn("ARXIV_COMPANION_REMOTE is not set", log)
        self.stub_python(0)
        rc, _ = self.run_once()
        self.assertEqual(rc, 0)

    def test_an_ssh_remote_without_a_key_warns_and_exits_zero(self):
        self.env["ARXIV_COMPANION_REMOTE"] = "git@example.invalid:owner/companion.git"
        self.env["ARXIV_COMPANION_SSH_KEY"] = (self.tmp / "nokey").as_posix()
        p = subprocess.run([BASH, (self.repo / "deploy" / "archive_run.sh").as_posix()],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("::warning::archive_run: no deploy key", p.stdout)
        self.assertFalse((self.state / "companion").exists())

    def test_a_reachable_companion_gets_this_run_and_survives_a_race(self):
        bare = self.tmp / "companion.git"
        self.git("init", "-q", "--bare", str(bare), cwd=self.tmp)
        self.env["ARXIV_COMPANION_REMOTE"] = bare.as_posix()
        self.env["ARXIV_COMPANION_DIR"] = (self.tmp / "checkout").as_posix()

        self.stub_python(1)
        rc, log = self.run_once()
        self.assertEqual(rc, 1)
        self.assertIn("pushed to the companion", log)

        tree = self.git("ls-tree", "-r", "--name-only", "main", cwd=bare).stdout.split()
        runs = sorted({p.split("/")[2] for p in tree if p.startswith("data/hotspot/")})
        self.assertEqual(len(runs), 1)
        run = runs[0]
        last = self.git("show", f"main:data/hotspot/{run}/last-run.json", cwd=bare).stdout
        self.assertIn('"generate": "failed"', last)
        runlog = self.git("show", f"main:data/hotspot/{run}/run.log", cwd=bare).stdout
        self.assertIn("generating", runlog)
        self.assertNotIn("yesterday", runlog, "the archive copied earlier runs, not this one")
        self.assertIn(f"data/hotspot/{run}/supervisor.log.tail", tree)
        author = self.git("log", "-1", "--format=%an", "main", cwd=bare).stdout.strip()
        self.assertEqual(author, "github-actions[bot]")

        # Something else lands on the companion; the next run must rebase, not give up.
        other = self.tmp / "other"
        self.git("clone", "-q", str(bare), str(other), cwd=self.tmp)
        (other / "note.txt").write_text("elsewhere\n", encoding="utf-8")
        self.git("add", "note.txt", cwd=other)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com",
                 "commit", "-q", "-m", "x", cwd=other)
        self.assertEqual(self.git("push", "-q", "origin", "HEAD:main", cwd=other).returncode, 0)

        time.sleep(1.1)
        self.stub_python(0)
        rc, _ = self.run_once()
        self.assertEqual(rc, 0)
        tree = self.git("ls-tree", "-r", "--name-only", "main", cwd=bare).stdout.split()
        self.assertIn("note.txt", tree)
        self.assertEqual(len({p.split("/")[2] for p in tree if p.startswith("data/hotspot/")}), 2)


if __name__ == "__main__":
    unittest.main()
