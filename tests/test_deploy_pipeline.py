"""The installer has to know what the host is before it claims to have installed.

The assets this replaces were a systemd service and timer hardcoded to
/opt/Daily-ArXiv-Assistant. They were never run once. Pointed at the first real
host, two things would have happened: the host turned out to be a container
whose PID 1 is tini, so `systemctl enable` was never going to work; and
run_hotspot.sh imported arxiv_assistant.hotspots.store, a module renamed to the
singular months earlier. A deploy script nobody runs is a deploy script nobody
tests.
"""

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import deploy.detect_target as dt

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"


def fake_which(*present):
    have = set(present)
    return lambda name: f"/usr/bin/{name}" if name in have else None


def fake_proc(comm: str, tmp: Path) -> Path:
    (tmp / "1").mkdir(parents=True, exist_ok=True)
    (tmp / "1" / "comm").write_text(comm + "\n", encoding="utf-8")
    return tmp


class SchedulerDetectionTests(unittest.TestCase):
    def test_systemctl_on_path_is_not_enough(self):
        # The container case, exactly: the binary can be in the image while
        # PID 1 is tini. Believing PATH here installs a timer that never runs
        # and reports success doing it.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = fake_proc("tini", Path(tmp))
            self.assertEqual(
                dt.detect_scheduler(fake_which("systemctl"), proc), dt.LOOP,
                "systemctl on PATH was taken as proof of an init system",
            )

    def test_real_systemd_is_used(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = fake_proc("systemd", Path(tmp))
            self.assertEqual(dt.detect_scheduler(fake_which("systemctl"), proc),
                             dt.SYSTEMD)

    def test_cron_is_the_second_choice(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = fake_proc("tini", Path(tmp))
            self.assertEqual(
                dt.detect_scheduler(fake_which("crontab", "cron"), proc), dt.CRON)

    def test_a_host_with_neither_still_gets_an_answer(self):
        # "No scheduler" is a supported outcome, not a failure: the supervised
        # loop covers it. Raising here would block the only host we have.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            proc = fake_proc("tini", Path(tmp))
            self.assertEqual(dt.detect_scheduler(fake_which(), proc), dt.LOOP)


class BackendDetectionTests(unittest.TestCase):
    def test_importability_is_not_availability(self):
        # llmcall imports on any host with pip, then shells out to provider
        # CLIs. The gateway's own `auto` resolves on the import, so on a bare
        # container it picks a transport whose every call fails. Detection asks
        # for the CLIs instead.
        self.assertEqual(
            dt.detect_backend(fake_which(), {"OPENAI_API_KEY": "k",
                                             "OPENAI_BASE_URL": "u"}),
            "openai")

    def test_a_provider_cli_means_auto_is_safe(self):
        self.assertEqual(dt.detect_backend(fake_which("claude"), {}), "auto")

    def test_the_c_compiler_is_not_a_model_transport(self):
        # `cc` is the llmcall chain's alias for Claude Code and also the C
        # compiler POSIX requires on every Unix. Probing the name reported a
        # working transport on the first host this ran against: a container
        # with no model CLI at all, where /usr/bin/cc is gcc 14. The backend
        # would have been pinned to llmcall and every call would have failed.
        self.assertEqual(
            dt.detect_backend(fake_which("cc", "gcc", "make"), {}), "none",
            "a host with a C compiler and no model CLI was reported as able "
            "to reach a model",
        )

    def test_no_transport_at_all_is_a_blocker(self):
        data = dt.report(fake_which("python3", "git"), Path("/nonexistent"), {})
        self.assertEqual(data["backend"], "none")
        self.assertTrue(any("model transport" in b for b in data["blockers"]),
                        "a host that cannot reach a model was not flagged")

    def test_missing_python_or_git_is_a_blocker(self):
        data = dt.report(fake_which("claude"), Path("/nonexistent"), {})
        self.assertTrue(any("python3" in b for b in data["blockers"]))
        self.assertTrue(any("git" in b for b in data["blockers"]))


class DeployScriptTests(unittest.TestCase):
    SCRIPTS = ("install.sh", "run_once.sh", "publish.sh", "supervise.sh", "status.sh")

    def test_no_script_references_a_module_that_was_renamed(self):
        # The retired runner imported arxiv_assistant.hotspots.store for months
        # after the package became singular, and nothing caught it because the
        # script was never executed.
        packages = {p.name for p in (REPO_ROOT / "arxiv_assistant").iterdir()
                    if p.is_dir() and not p.name.startswith("__")}
        for name in self.SCRIPTS:
            text = (DEPLOY / name).read_text(encoding="utf-8")
            for ref in re.findall(r"arxiv_assistant\.([A-Za-z_][A-Za-z0-9_]*)", text):
                with self.subTest(script=name, module=ref):
                    self.assertIn(
                        ref, packages | {"utils"},
                        "%s imports arxiv_assistant.%s, which does not exist"
                        % (name, ref),
                    )

    def test_nothing_hardcodes_an_install_path(self):
        # The point of the rewrite: the same scripts have to land on a
        # container at /workspace and a VPS at /opt without editing.
        for name in self.SCRIPTS:
            text = (DEPLOY / name).read_text(encoding="utf-8")
            code = "\n".join(l for l in text.splitlines()
                             if not l.lstrip().startswith("#"))
            with self.subTest(script=name):
                self.assertNotIn(
                    "/opt/Daily-ArXiv-Assistant", code,
                    "%s hardcodes the old install path" % name)

    def test_the_installer_refuses_to_finish_without_a_live_model_call(self):
        # "Installed" has to mean "will work", not "files were copied". The
        # selftest is the only step that can prove the host reaches a model.
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("llm_gateway.call", text,
                      "the installer never proves a model answers")
        selftest = text.split("proving the model transport answers", 1)[1]
        scheduling = selftest.split("6/6", 1)[0]
        self.assertIn("exit 1", scheduling,
                      "a failed selftest does not stop the install")

    def test_the_env_is_loaded_before_the_host_is_judged(self):
        # Backend detection reads OPENAI_API_KEY and OPENAI_BASE_URL from the
        # environment. Detecting first reports "no model transport" on a host
        # whose credentials are sitting in the env file two lines away, and the
        # installer then refuses an install that was going to work.
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        source_env = text.index('. "$ENV_FILE"')
        detect = text.index("detect_target.py")
        self.assertLess(source_env, detect,
                        "the host is judged before its credentials are loaded")

    def test_the_pinned_backend_reaches_the_selftest(self):
        # The pin is APPENDED to the env file after it was sourced. Without a
        # re-source the selftest runs on whatever `auto` resolves to, so it
        # passes or fails for a different reason than the one being installed.
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        pin = text.index("ARXIV_ASSISTANT_LLM_BACKEND=$BACKEND")
        selftest = text.index("llm_gateway.call")
        between = text[pin:selftest]
        self.assertIn('. "$ENV_FILE"', between,
                      "the backend is pinned but never re-read, so the "
                      "selftest does not exercise it")

    def test_run_once_records_evidence_even_when_it_fails(self):
        # A timer swallows exit codes and the supervisor outlives the run, so
        # artifacts are the only place a failure can be seen afterwards.
        text = (DEPLOY / "run_once.sh").read_text(encoding="utf-8")
        self.assertIn("last-run.json", text)
        self.assertIn("heartbeat", text)
        body = text.split("T1=$(date +%s)", 1)[1]
        self.assertLess(
            body.index("heartbeat"), body.index("exit 1"),
            "the heartbeat is written after the failure exit, so a failing run "
            "looks identical to a run that never happened",
        )


if __name__ == "__main__":
    unittest.main()
