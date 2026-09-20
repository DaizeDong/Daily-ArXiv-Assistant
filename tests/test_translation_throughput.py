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

    def test_an_echoed_field_is_actually_sent_again(self):
        # The two "already translated" tests have to agree. _has_zh_fields picks
        # a day BECAUSE a _zh merely repeats its source; if the field collector
        # then skips that field BECAUSE a _zh is present, the day is rewritten
        # unchanged and reselected on every future run, translating nothing. It
        # never fails, never logs anything odd, and never finishes. Measured on
        # 2026-09-20: 27 days rewritten, zero new _zh fields in the ones that
        # had been echoed.
        sent = []

        def recording(model, messages):
            import json as _json
            payload = _json.loads(messages[-1]["content"])
            sent.extend(payload)
            return _json.dumps(["译" for _ in payload], ensure_ascii=False)

        data = {"featured_topics": [{
            "headline": "A model ships",
            "headline_zh": "A model ships",          # echoed: not a translation
            "summary": "Real summary",
            "summary_zh": "真实摘要",                 # genuinely translated
        }]}

        with mock.patch.object(tr, "_chat", recording):
            out = tr.collect_and_translate(data, model="x")

        self.assertIn("A model ships", sent,
                      "the echoed field was skipped, so the day can never heal")
        self.assertNotIn("Real summary", sent,
                         "a field that was genuinely translated was paid for twice")
        self.assertEqual(out["featured_topics"][0]["headline_zh"], "译")
        self.assertEqual(out["featured_topics"][0]["summary_zh"], "真实摘要")

    def test_a_day_whose_zh_only_repeats_the_english_is_retried(self):
        # Days frozen by the earlier fallback have to heal by themselves; there
        # are months of them and nobody is going to find them by hand.
        frozen = {"featured_topics": [
            {"headline": "A model ships", "headline_zh": "A model ships"},
        ]}
        real = {"featured_topics": [
            {"headline": "A model ships", "headline_zh": "一个模型发布"},
        ]}
        self.assertFalse(tr._has_zh_fields(frozen),
                         "a _zh that repeats its source counted as translated")
        self.assertTrue(tr._has_zh_fields(real))

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
