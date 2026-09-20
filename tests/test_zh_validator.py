"""The translation validator has to be able to fail.

It could not. It scanned out/hot/web_data for `<day>_zh.json` companion files,
a scheme nothing has ever written -- there are zero such files on auto_update --
while the translator writes _zh fields inline into out/web_data/hot/<day>.json.
So it iterated an empty directory and printed that every asset was present and
non-trivial, on every run, for months. A green that means "nothing was examined"
looks exactly like a green that means "nothing is wrong".

These tests pin both directions: the shapes it must reject, and the shapes it
must not, because a validator that fails on a healthy partial run would block
the step that saves the work.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import scripts.validate_zh_assets as validator


def _topic(headline, zh=None, **extra):
    topic = {"headline": headline, "summary": "some summary"}
    if zh is not None:
        topic["headline_zh"] = zh
    topic.update(extra)
    return topic


class ValidatorTests(unittest.TestCase):
    def _run(self, days: dict | None):
        with tempfile.TemporaryDirectory() as tmp:
            hot = Path(tmp) / "hot"
            if days is not None:
                hot.mkdir(parents=True)
                for name, payload in days.items():
                    (hot / name).write_text(json.dumps(payload, ensure_ascii=False),
                                            encoding="utf-8")
            with mock.patch.object(validator, "WEB_DATA", hot):
                return validator.main()

    def test_a_missing_directory_fails_rather_than_passing_empty(self):
        self.assertEqual(self._run(None), 1)

    def test_an_empty_directory_fails(self):
        self.assertEqual(self._run({}), 1)

    def test_nothing_translated_at_all_fails(self):
        days = {"2026-05-01.json": {"featured_topics": [_topic("A model ships")]}}
        self.assertEqual(
            self._run(days), 1,
            "a run in which no day carries a translation reported success",
        )

    def test_a_partly_translated_archive_passes(self):
        # The translator stops on a deadline so that what it finished can be
        # saved. Failing here would block the step that saves it.
        days = {
            "2026-05-01.json": {"featured_topics": [_topic("A", zh="甲")]},
            "2026-05-02.json": {"featured_topics": [_topic("B")]},
            "2026-05-03.json": {"featured_topics": [_topic("C")]},
        }
        self.assertEqual(self._run(days), 0)

    def test_a_zh_list_that_does_not_line_up_fails(self):
        # Rendering a three-item translation against four source bullets is
        # worse than showing the English.
        days = {"2026-05-01.json": {"featured_topics": [
            _topic("A", zh="甲",
                   why_it_matters=["one", "two", "three"],
                   why_it_matters_zh=["一", "二"]),
        ]}}
        self.assertEqual(self._run(days), 1)

    def test_an_echoed_translation_is_reported_but_not_fatal(self):
        # The model does echo a string sometimes, and the translator treats such
        # a field as untranslated and retries it, so this heals by itself.
        days = {
            "2026-05-01.json": {"featured_topics": [_topic("A model ships",
                                                          zh="A model ships")]},
            "2026-05-02.json": {"featured_topics": [_topic("B", zh="乙")]},
        }
        self.assertEqual(self._run(days), 0)

    def test_it_reads_the_directory_the_translator_writes(self):
        # The original bug in one assertion: the validator and the translator
        # have to be pointed at the same place.
        import scripts.translate_hotspot_web_data as tr

        self.assertEqual(
            validator.WEB_DATA.relative_to(validator.REPO_ROOT),
            Path("out") / "web_data" / "hot",
        )
        self.assertIn('"out" / "web_data" / "hot"',
                      Path(tr.__file__).read_text(encoding="utf-8").replace("'", '"'))


if __name__ == "__main__":
    unittest.main()
