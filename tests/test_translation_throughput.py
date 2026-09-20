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
