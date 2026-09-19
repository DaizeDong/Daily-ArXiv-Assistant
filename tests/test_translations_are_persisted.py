"""Translation is the expensive step, so its output must outlive the runner.

The Pages workflow restores a clean out/ from auto_update on every run, adds
_zh fields to it, builds the site, and then discards the workspace. For months
nothing wrote those translations back: auto_update carried _zh only through
2026-04-04, from a one-off backfill, so every build retranslated roughly 160
days of archive to produce a result it immediately threw away.

That was affordable while the provider chain answered in about 24 seconds a
day. It stopped being affordable when the chain slowed down: six hours bought
eighteen days, the job hit the 360-minute limit, and the next run began again
at the same date. The site could not publish, and no step ever reported a
failure -- the run was `cancelled`, which reads like nothing went wrong.

Two invariants keep that from coming back. Expensive output is persisted, and
the budget handed to the provider chain is large enough for the answers this
repository actually measured.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish_md.yml"
TRANSLATE = REPO_ROOT / "scripts" / "translate_hotspot_web_data.py"

#: Measured in this repository and recorded in commit 3cc3bb5: successful
#: answers had a median of 134s and a p95 of 428s. A whole-chain budget below
#: the p95 cannot fit one slow answer plus the fallback leg that justifies
#: having a chain at all.
MEASURED_P95_SECONDS = 428


class TranslationsArePersistedTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.script = TRANSLATE.read_text(encoding="utf-8")

    def test_the_workflow_writes_translations_back_to_auto_update(self):
        self.assertRegex(
            self.workflow, r"(?m)^\s*-\s*name:.*Persist translations",
            "nothing carries translations out of the runner; every build will "
            "retranslate the whole archive and discard the result",
        )
        persist = self.workflow.split("Persist translations", 1)[1]
        persist = persist.split("- name:", 1)[0]
        self.assertIn(
            "HEAD:auto_update", persist,
            "the persist step does not push to auto_update, which is the branch "
            "the next build is seeded from",
        )
        self.assertIn(
            "[skip ci]", persist,
            "a push to auto_update without [skip ci] retriggers this very "
            "workflow, whose own trigger list includes that branch's consumers",
        )

    def test_the_job_may_actually_write(self):
        # contents: read makes the persist step fail at push time rather than at
        # review time, and the failure arrives an hour into the build.
        block = re.search(r"(?ms)^permissions:\n((?:[ \t]+.*\n|\n)*)", self.workflow)
        self.assertIsNotNone(block, "no permissions block")
        self.assertRegex(
            block.group(1), r"(?m)^\s*contents:\s*write",
            "the job persists translations but is only granted contents: read",
        )

    def test_the_persist_step_never_discards_the_other_writer(self):
        persist = self.workflow.split("Persist translations", 1)[1]
        persist = persist.split("- name:", 1)[0]
        self.assertNotIn(
            "--force", persist,
            "the daily pipeline writes to auto_update too; forcing would drop "
            "its output to win a race",
        )
        self.assertIn(
            "--rebase", persist,
            "losing a race against the daily pipeline is ordinary here, so the "
            "step has to reconcile rather than fail the publish",
        )

    def test_the_chain_budget_fits_a_measured_answer(self):
        budget = re.search(r"(?m)^CHAIN_BUDGET_S\s*=\s*(\d+)", self.script)
        self.assertIsNotNone(budget, "the budget is not a named, greppable value")
        self.assertGreater(
            int(budget.group(1)), MEASURED_P95_SECONDS,
            "the whole-chain budget is below this repository's own measured p95, "
            "so a slow first provider leaves the next leg a remainder it cannot "
            "use and the call fails without ever having been given a chance",
        )

    def test_the_call_site_uses_the_named_budget(self):
        self.assertRegex(
            self.script, r"timeout_s=CHAIN_BUDGET_S",
            "the call site hardcodes a timeout again, so the named constant and "
            "the value actually used can drift apart",
        )


if __name__ == "__main__":
    unittest.main()
