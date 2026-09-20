"""The translation run has to fit inside the job that contains it.

Measured on 2026-09-19, twice: six hours of translating bought eighteen days
and then twenty-three, out of a backlog of about a hundred and sixty. Both runs
were killed at the 360-minute job limit, and a killed job skips every later
step -- including the one that pushes what was translated. Two full runs of
model calls were discarded, and the next run began again at the same date.

Neither speed nor the deadline alone is enough. Without concurrency a day costs
about a quarter of an hour, because it is roughly six calls and a call has a
median of 134s here, so the backlog cannot drain. Without a deadline a run is
killed before it can save what it did, so no amount of speed accumulates.
"""

import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import scripts.translate_hotspot_web_data as tr


class ConcurrencyTests(unittest.TestCase):
    def test_a_days_batches_are_sent_at_the_same_time(self):
        # The batches of one day are disjoint slices of one list. Sending them
        # one at a time multiplies the slowest part of the job by six.
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def fake_chat(model, messages):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.15)
            with lock:
                in_flight -= 1
            import json as _json
            payload = _json.loads(messages[-1]["content"])
            return _json.dumps(["译" for _ in payload], ensure_ascii=False)

        texts = [f"english text {i}" for i in range(24)]
        with mock.patch.object(tr, "_chat", fake_chat), \
             mock.patch.object(tr, "BATCH_CONCURRENCY", 6):
            out = tr.batch_translate(texts, model="x", batch_size=4)

        self.assertEqual(out, ["译"] * 24)
        self.assertGreater(
            peak, 1,
            "batches were sent one at a time; a day then costs the sum of its "
            "calls rather than the slowest of them",
        )

    def test_concurrency_never_exceeds_the_configured_limit(self):
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def fake_chat(model, messages):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            import json as _json
            payload = _json.loads(messages[-1]["content"])
            return _json.dumps(["译" for _ in payload], ensure_ascii=False)

        texts = [f"english text {i}" for i in range(40)]
        with mock.patch.object(tr, "_chat", fake_chat), \
             mock.patch.object(tr, "BATCH_CONCURRENCY", 3):
            tr.batch_translate(texts, model="x", batch_size=4)

        self.assertLessEqual(peak, 3, "the concurrency limit is not respected")

    def test_a_batch_that_never_answers_records_nothing(self):
        # Not the English text: written into a _zh field it reads as a finished
        # translation, and the next run skips a day on exactly that signal, so
        # one failed batch used to freeze its day forever. None leaves the field
        # absent, the site falls back to English as it always has, and the day
        # is picked up again next time.
        def always_fails(model, messages):
            raise RuntimeError("chain down")

        texts = ["english one", "english two"]
        with mock.patch.object(tr, "_chat", always_fails), \
             mock.patch.object(tr, "BATCH_ATTEMPTS", 1):
            out = tr.batch_translate(texts, model="x", batch_size=2)

        self.assertEqual(out, [None, None])

    def test_an_answer_that_equals_its_source_is_not_sent_again(self):
        # A _zh equal to its source is usually the CORRECT answer: the source is
        # already Chinese, or it is a repository slug or product name the prompt
        # tells the model to leave alone. Measured on 2026-09-20, all 702 fields
        # a stricter rule called untranslated were of those two kinds, and no
        # field anywhere was missing its key. Re-sending them cannot change the
        # answer; it just spends the model on the same strings every run.
        sent = []

        def recording(model, messages):
            import json as _json
            payload = _json.loads(messages[-1]["content"])
            sent.extend(payload)
            return _json.dumps(["译" for _ in payload], ensure_ascii=False)

        data = {"featured_topics": [{
            "headline": "larksuite/cli",
            "headline_zh": "larksuite/cli",   # a repo slug: unchanged is correct
            "summary": "Real summary",
            "summary_zh": "真实摘要",
            "why_it_matters": "Needs work",   # no _zh at all: this one is pending
        }]}

        with mock.patch.object(tr, "_chat", recording):
            out = tr.collect_and_translate(data, model="x")

        self.assertEqual(
            sent, ["Needs work"],
            "only the field with no answer should be sent; got %r" % (sent,),
        )
        self.assertEqual(out["featured_topics"][0]["headline_zh"], "larksuite/cli")
        self.assertEqual(out["featured_topics"][0]["why_it_matters_zh"], "译")

    def test_a_missing_key_is_what_marks_a_day_as_pending(self):
        # Absent means the batch failed and recorded nothing, which is the only
        # state worth retrying. Present means the model answered, whatever it
        # answered.
        answered = {"featured_topics": [
            {"headline": "larksuite/cli", "headline_zh": "larksuite/cli"},
        ]}
        pending = {"featured_topics": [
            {"headline": "A model ships"},
        ]}
        self.assertTrue(tr._has_zh_fields(answered),
                        "a correct unchanged answer was treated as pending, "
                        "which never converges")
        self.assertFalse(tr._has_zh_fields(pending))

    def test_a_real_translation_replaces_one_that_only_echoes(self):
        # The merge used to write only absent keys, so a genuine translation
        # arriving for a field poisoned by the old fallback had nowhere to land.
        # Four were dropped that way on 2026-09-20 and the run reported nothing
        # to persist.
        import scripts.rebuild_hotspot_web_data as rb

        data = {"featured_topics": [{
            "headline": "A model ships",
            "headline_zh": "A model ships",   # echo left by the old fallback
            "summary": "Some summary",
            "summary_zh": "已有译文",
        }], "category_sections": [], "long_tail_sections": [], "watchlist": []}

        rb._merge_zh(data, {"A model ships": {
            "headline_zh": "一个模型发布",
            "summary_zh": "另一个译文",
        }})

        topic = data["featured_topics"][0]
        self.assertEqual(topic["headline_zh"], "一个模型发布",
                         "a real translation could not replace an echo")
        self.assertEqual(topic["summary_zh"], "已有译文",
                         "an existing real translation was overwritten")

    def test_chinese_text_is_never_sent_to_the_model(self):
        sent = []

        def recording(model, messages):
            import json as _json
            payload = _json.loads(messages[-1]["content"])
            sent.extend(payload)
            return _json.dumps(["译" for _ in payload], ensure_ascii=False)

        with mock.patch.object(tr, "_chat", recording):
            out = tr.batch_translate(["已经是中文了", "english text"],
                                     model="x", batch_size=8)

        self.assertEqual(sent, ["english text"])
        self.assertEqual(out[0], "已经是中文了")


class DeadlineTests(unittest.TestCase):
    def _targets(self, tmp: Path, days: int):
        hot = tmp / "out" / "web_data" / "hot"
        hot.mkdir(parents=True)
        for i in range(days):
            (hot / f"2026-05-{i + 1:02d}.json").write_text("{}", encoding="utf-8")
        return hot

    def test_an_exhausted_deadline_stops_before_the_next_day(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._targets(root, 4)
            translated = []

            with mock.patch.object(tr, "REPO_ROOT", root), \
                 mock.patch.object(tr, "DEADLINE_MINUTES", 0), \
                 mock.patch.object(tr, "translate_file",
                                   lambda p, m: translated.append(p.stem)), \
                 mock.patch("sys.argv", ["translate"]):
                tr.main()

            self.assertEqual(
                translated, [],
                "work started after the deadline had passed; the job that "
                "contains this will be killed before it can be saved",
            )

    def test_a_live_deadline_does_not_stop_anything(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._targets(root, 3)
            translated = []

            with mock.patch.object(tr, "REPO_ROOT", root), \
                 mock.patch.object(tr, "DEADLINE_MINUTES", 60), \
                 mock.patch.object(tr, "translate_file",
                                   lambda p, m: translated.append(p.stem)), \
                 mock.patch("sys.argv", ["translate"]):
                tr.main()

            self.assertEqual(len(translated), 3)

    def test_days_are_translated_at_the_same_time(self):
        # Days are independent: each reads one file, calls the model for its own
        # fields, and writes that file back. Running them one after another left
        # only BATCH_CONCURRENCY calls in flight while hundreds waited, which is
        # where an eleven hour estimate came from.
        import tempfile

        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def slow_day(path, model):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.1)
            with lock:
                in_flight -= 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._targets(root, 8)
            with mock.patch.object(tr, "REPO_ROOT", root), \
                 mock.patch.object(tr, "DEADLINE_MINUTES", 60), \
                 mock.patch.object(tr, "DAY_CONCURRENCY", 4), \
                 mock.patch.object(tr, "translate_file", slow_day), \
                 mock.patch("sys.argv", ["translate"]):
                tr.main()

        self.assertGreater(peak, 1, "days were translated one at a time")
        self.assertLessEqual(peak, 4, "the day concurrency limit is not respected")

    def test_days_already_running_at_the_deadline_are_allowed_to_finish(self):
        # The deadline gates admission, not completion. Killing a day mid-flight
        # throws away everything it has already paid for, and a half-translated
        # day written back reads as translated to the next run, which skips on
        # exactly that signal -- so its untranslated half would never be redone.
        import tempfile

        started = []
        finished = []
        lock = threading.Lock()

        def slow_day(path, model):
            with lock:
                started.append(path.stem)
            time.sleep(0.3)
            with lock:
                finished.append(path.stem)

        import contextlib
        import io

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._targets(root, 6)
            # A deadline that expires while the first wave is still running.
            with mock.patch.object(tr, "REPO_ROOT", root), \
                 mock.patch.object(tr, "DEADLINE_MINUTES", 0.002), \
                 mock.patch.object(tr, "DAY_CONCURRENCY", 2), \
                 mock.patch.object(tr, "translate_file", slow_day), \
                 mock.patch("sys.argv", ["translate"]), \
                 contextlib.redirect_stdout(out):
                tr.main()

        # That a started day runs to completion is guaranteed by the executor,
        # not by this file -- a running thread cannot be cancelled, so asserting
        # it here could never fail. What this file does control is the
        # accounting: every day the deadline admitted has to be counted and
        # reported, or the next run is told to resume from the wrong place and
        # the difference is silently retranslated or silently skipped.
        self.assertEqual(sorted(started), sorted(finished))
        self.assertLess(len(started), 6, "the deadline admitted every day anyway")
        self.assertIn(
            "Translated %d day(s)" % len(finished), out.getvalue(),
            "the run reported a different number of days than it actually "
            "completed: %s" % out.getvalue().strip().splitlines()[-1:],
        )
        self.assertIn(
            "with %d day(s) left" % (6 - len(started)), out.getvalue(),
            "the reported remainder does not match what was left unadmitted",
        )

    def test_the_deadline_leaves_the_job_room_to_finish(self):
        # The job is killed at 360 minutes and the steps after translation
        # (validate, persist, build, render, deploy) need their own time.
        self.assertLess(
            tr.DEADLINE_MINUTES, 330,
            "the deadline leaves no room for the steps that save and publish "
            "what was translated",
        )


if __name__ == "__main__":
    unittest.main()
