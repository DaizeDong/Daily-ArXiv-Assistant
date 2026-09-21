"""A build that runs for an hour must not be cancellable by the next trigger.

2026-09-15 to 2026-09-19: the published site went stale for four days. Five
consecutive Pages builds died at the translation step with conclusion
`cancelled`, not `failure`, so nothing looked broken -- no error, no failing
step, just a run that stopped. The cause was `cancel-in-progress: true` on a
67-minute build whose triggers include pushes to main under `arxiv_assistant/**`
and `scripts/**`. Routine development on main was killing the publish.

`cancel-in-progress` is right for a fast check where only the newest commit
matters. It is wrong for a slow publish, which can then never reach the end.
The threshold below is deliberately crude: any workflow that talks to a model
per item is in the slow class.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: Workflows whose runtime is dominated by per-item model calls.
SLOW_WORKFLOWS = ("publish_md.yml", "weekly_digest.yaml")


def _concurrency_block(text: str) -> str:
    match = re.search(r"(?ms)^concurrency:\n((?:[ \t]+.*\n|\n)*)", text)
    return match.group(1) if match else ""


class LongBuildsAreNotCancelledTests(unittest.TestCase):
    def test_slow_workflows_queue_rather_than_cancel(self):
        for name in SLOW_WORKFLOWS:
            path = WORKFLOW_DIR / name
            with self.subTest(workflow=name):
                self.assertTrue(path.exists(), "%s is gone; update this test" % name)
                block = _concurrency_block(path.read_text(encoding="utf-8"))
                self.assertTrue(block.strip(), "%s has no concurrency block" % name)
                setting = re.search(r"(?m)^\s*cancel-in-progress:\s*(\S+)", block)
                self.assertIsNotNone(
                    setting, "%s does not state cancel-in-progress" % name
                )
                self.assertEqual(
                    setting.group(1), "false",
                    "%s can be cancelled mid-build. This run takes about an hour "
                    "and its own triggers fire more often than that, so it would "
                    "never reach the end -- and it would report `cancelled`, "
                    "which reads like nothing went wrong." % name,
                )

    def test_the_site_can_rebuild_without_the_paper_pipeline(self):
        # Hotspots are generated off-site and pushed to auto_update with
        # [skip ci], so that push triggers nothing. If the only other trigger
        # is the paper chain, the site rebuilds only on days arXiv announces --
        # and it announces nothing at weekends, so Saturday and Sunday hotspots
        # would sit on the branch until Monday.
        text = (WORKFLOW_DIR / "publish_md.yml").read_text(encoding="utf-8")
        on_block = text.split("\njobs:", 1)[0]
        self.assertIn("schedule:", on_block,
                      "the published site has no trigger of its own")
        self.assertRegex(on_block, r"cron:\s*'[^']+'",
                         "the schedule names no time")

    def test_tar_writing_a_windows_path_forces_local(self):
        # These jobs run on Windows runners, where RUNNER_TEMP is an absolute
        # path starting with a drive letter. GNU tar reads the "C:" in it as the
        # host half of a host:path remote spec, answers "Cannot connect to C:
        # resolve failed", and exits 128. Reproduced and fixed locally before
        # this was written: the same command fails without the flag and writes
        # the archive with it.
        for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
            text = path.read_text(encoding="utf-8")
            for block in re.findall(r"(?ms)^\s*run: \|\n(.*?)(?=\n\s*- |\n\s*\w+:\n|\Z)", text):
                if not re.search(r"(?m)^\s*tar\b", block):
                    continue
                if not re.search(r"RUNNER_TEMP|runner\.temp", block):
                    continue
                # Comments out, or this passes on the comment that explains the
                # flag rather than on the flag. Checked by deleting the flag and
                # watching the first version of this test stay green.
                command = "\n".join(line for line in block.splitlines()
                                    if not line.lstrip().startswith("#"))
                with self.subTest(workflow=path.name):
                    self.assertIn(
                        "--force-local", command,
                        "%s tars into a Windows path without --force-local, so "
                        "tar will treat the drive letter as a hostname and the "
                        "step will fail with exit 128" % path.name,
                    )

    def test_every_workflow_that_sets_concurrency_says_which_it_wants(self):
        # An omitted cancel-in-progress defaults to false, which is the safe
        # side -- but silence makes the two classes indistinguishable to a
        # reader deciding whether a given build may be interrupted.
        for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
            text = path.read_text(encoding="utf-8")
            block = _concurrency_block(text)
            if not block.strip():
                continue
            with self.subTest(workflow=path.name):
                self.assertRegex(
                    block, r"(?m)^\s*cancel-in-progress:",
                    "%s groups runs but never says whether one may be killed "
                    "mid-flight" % path.name,
                )


if __name__ == "__main__":
    unittest.main()
