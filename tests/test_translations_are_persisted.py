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

Three invariants keep that from coming back. The expensive output is persisted;
persisting it never costs the daily pipeline its own work; and the budget handed
to the provider chain is large enough for the answers this repository measured.
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish_md.yml"
TRANSLATE = REPO_ROOT / "scripts" / "translate_hotspot_web_data.py"
PERSIST = REPO_ROOT / "scripts" / "persist_zh_translations.py"

#: Measured in this repository and recorded in commit 3cc3bb5: successful
#: answers had a median of 134s and a p95 of 428s. A whole-chain budget below
#: the p95 cannot fit one slow answer plus the fallback leg that justifies
#: having a chain at all.
MEASURED_P95_SECONDS = 428


def _persist_step(workflow_text: str) -> str:
    body = workflow_text.split("Persist translations", 1)[1]
    return body.split("      - name:", 1)[0]


def _day(topics):
    return {"featured_topics": topics, "category_sections": [],
            "long_tail_sections": [], "watchlist": []}


class WorkflowInvariantTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.script = TRANSLATE.read_text(encoding="utf-8")

    def test_the_workflow_writes_translations_back_to_auto_update(self):
        self.assertRegex(
            self.workflow, r"(?m)^\s*-\s*name:.*Persist translations",
            "nothing carries translations out of the runner; every build will "
            "retranslate the whole archive and discard the result",
        )
        step = _persist_step(self.workflow)
        self.assertIn(
            "HEAD:auto_update", step,
            "the persist step does not push to auto_update, the branch the next "
            "build is seeded from",
        )
        self.assertIn(
            "[skip ci]", step,
            "a push to auto_update without [skip ci] retriggers this very "
            "workflow, whose trigger list includes that branch's consumers",
        )

    def test_the_job_may_actually_write(self):
        # contents: read makes the persist step fail at push time rather than at
        # review time, and that failure arrives an hour into the build.
        block = re.search(r"(?ms)^permissions:\n((?:[ \t]+.*\n|\n)*)", self.workflow)
        self.assertIsNotNone(block, "no permissions block")
        self.assertRegex(
            block.group(1), r"(?m)^\s*contents:\s*write",
            "the job persists translations but is granted only contents: read",
        )

    def test_persisting_never_costs_the_other_writer_its_work(self):
        step = _persist_step(self.workflow)
        self.assertNotIn(
            "--force", step,
            "the daily pipeline writes to this branch too; forcing would drop "
            "its output to win a race",
        )
        self.assertIn(
            "reset --hard origin/auto_update", step,
            "the step must reconcile against what the branch holds now. Our "
            "copies are an hour old by the time this runs.",
        )
        self.assertIn(
            "persist_zh_translations.py", step,
            "the step copies whole files instead of moving only the _zh fields, "
            "so it would revert whatever the daily pipeline wrote meanwhile",
        )

    def test_the_chain_budget_fits_a_measured_answer(self):
        budget = re.search(r"(?m)^CHAIN_BUDGET_S\s*=\s*(\d+)", self.script)
        self.assertIsNotNone(budget, "the budget is not a named, greppable value")
        self.assertGreater(
            int(budget.group(1)), MEASURED_P95_SECONDS,
            "the whole-chain budget is below this repository's own measured p95, "
            "so a slow first provider leaves the next leg a remainder it cannot "
            "use and the call fails without having been given a chance",
        )

    def test_the_call_site_uses_the_named_budget(self):
        self.assertRegex(
            self.script, r"timeout_s=CHAIN_BUDGET_S",
            "the call site hardcodes a timeout again, so the named constant and "
            "the value actually used can drift apart",
        )


class PersistMergeTests(unittest.TestCase):
    """What the merge does to data, not merely what the workflow says."""

    def _run(self, source: dict, target: dict):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir, tgt_dir = Path(tmp) / "src", Path(tmp) / "tgt"
            src_dir.mkdir()
            tgt_dir.mkdir()
            for name, payload in source.items():
                (src_dir / name).write_text(json.dumps(payload, ensure_ascii=False),
                                            encoding="utf-8")
            for name, payload in target.items():
                (tgt_dir / name).write_text(json.dumps(payload, ensure_ascii=False),
                                            encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(PERSIST), "--source", str(src_dir),
                 "--target", str(tgt_dir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )
            written = {p.name: json.loads(p.read_text(encoding="utf-8"))
                       for p in tgt_dir.glob("*.json")}
            return proc, written

    def test_translations_land_on_the_targets_own_content(self):
        # The daily pipeline rewrote the summary while we were translating. Its
        # text must survive; only the _zh field comes across.
        source = {"2026-05-01.json": _day([
            {"headline": "A model ships", "summary": "old text",
             "headline_zh": "一个模型发布"},
        ])}
        target = {"2026-05-01.json": _day([
            {"headline": "A model ships", "summary": "rewritten by the daily run"},
        ])}
        proc, written = self._run(source, target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        topic = written["2026-05-01.json"]["featured_topics"][0]
        self.assertEqual(topic["headline_zh"], "一个模型发布")
        self.assertEqual(
            topic["summary"], "rewritten by the daily run",
            "the merge reverted the other writer's content to our stale copy",
        )

    def test_a_topic_the_daily_run_dropped_is_not_resurrected(self):
        source = {"2026-05-01.json": _day([
            {"headline": "Retracted item", "headline_zh": "已撤回"},
        ])}
        target = {"2026-05-01.json": _day([])}
        proc, written = self._run(source, target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            written["2026-05-01.json"]["featured_topics"], [],
            "a topic removed upstream came back because we carried our copy over",
        )

    def test_a_day_missing_from_the_target_is_not_recreated(self):
        source = {"2026-05-02.json": _day([
            {"headline": "Only here", "headline_zh": "只在此处"},
        ])}
        proc, written = self._run(source, {})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(written, {}, "a deleted day was resurrected")

    def test_running_twice_writes_nothing_the_second_time(self):
        # An empty-meaning commit on every build is noise that trains people to
        # stop reading this branch's history.
        topic = {"headline": "Stable", "summary": "s", "headline_zh": "稳定"}
        source = {"2026-05-03.json": _day([dict(topic)])}
        target = {"2026-05-03.json": _day([dict(topic)])}
        proc, _ = self._run(source, target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("across 0 day(s)", proc.stdout)

    def test_a_missing_directory_is_an_error_not_an_empty_run(self):
        # Persisting nothing quietly is exactly how the work came to be redone
        # on every build for months.
        proc = subprocess.run(
            [sys.executable, str(PERSIST), "--source", "does-not-exist",
             "--target", "also-not"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
