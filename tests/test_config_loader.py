import configparser
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from arxiv_assistant.utils.config_loader import CONFIG_DIR, CONFIG_FILES, load_repo_config

PAPER_SECTIONS = ("SELECTION", "FILTERING", "PAPER_FILTER", "OUTPUT", "MONTHLY_SUMMARY", "READER", "LLM")
HOTSPOT_SECTIONS = ("HOTSPOTS", "HOTSPOT_SOURCES", "HOTSPOT_X", "HOTSPOT_GITHUB",
                    "HOTSPOT_HN", "HOTSPOT_REUSE", "HOTSPOT_RUNTIME")


class ConfigLoaderTests(unittest.TestCase):
    def test_both_files_merge_into_one_config(self):
        config = load_repo_config()
        for section in PAPER_SECTIONS + HOTSPOT_SECTIONS:
            self.assertIn(section, config, "%s went missing when the config was split" % section)

    def test_the_split_actually_separates_the_two_subsystems(self):
        # If everything drifted back into one file the split bought nothing, and
        # the paper pipeline's settings are buried again.
        paper = configparser.ConfigParser()
        paper.read(CONFIG_DIR / "config.ini", encoding="utf-8")
        hot = configparser.ConfigParser()
        hot.read(CONFIG_DIR / "hotspot.ini", encoding="utf-8")
        self.assertFalse([s for s in paper.sections() if s.startswith("HOTSPOT")])
        self.assertTrue(all(s.startswith("HOTSPOT") for s in hot.sections()))

    def test_a_missing_file_fails_loudly_rather_than_defaulting(self):
        # configparser answers a missing section with whatever the caller passed
        # as a fallback, so a lost file would change what the feed collects
        # without breaking anything visibly. Refusing to start is the point.
        for dropped in CONFIG_FILES:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                for name in CONFIG_FILES:
                    if name != dropped:
                        shutil.copy(CONFIG_DIR / name, target / name)
                with self.assertRaises(FileNotFoundError, msg="%s may vanish silently" % dropped):
                    load_repo_config(target / CONFIG_FILES[0])

    def test_every_entry_point_sees_the_whole_config(self):
        # The three loaders that existed before this were not equivalent: one
        # read a relative path, one passed no encoding. Reading through any of
        # them must now give the same sections.
        from arxiv_assistant.environment import CONFIG
        from arxiv_assistant.hotspot.support.config import load_repo_config as hotspot_loader

        expected = set(load_repo_config().sections())
        self.assertEqual(set(CONFIG.sections()), expected)
        self.assertEqual(set(hotspot_loader().sections()), expected)


class WorkflowsUseTheLoaderTests(unittest.TestCase):
    """No workflow may read one config file and call that the configuration.

    cron_runs.yaml decided whether to generate hotspots by building its own
    ConfigParser over configs/config.ini and asking for [HOTSPOT_RUNTIME],
    which lives in configs/hotspot.ini since the split. It saw no such section
    and took its fallback. The answer was right by coincidence -- fallback and
    configured value are both "local" -- so nothing looked wrong, and setting
    runtime = actions would have been ignored without a word.
    """

    WORKFLOW_DIR = CONFIG_DIR.parent / ".github" / "workflows"

    def test_no_workflow_reads_configuration_from_one_file(self):
        # Reading a setting to decide something must go through the loader.
        # Editing one named file in place is a different act and stays allowed:
        # remedy_missed_dates.yml rewrites [SELECTION] in config.ini, which is
        # where that section lives.
        #
        # Comments are stripped first. The step this rule came from now carries
        # a comment naming ConfigParser to explain the bug, and the first
        # version of this test matched that comment -- green code, red prose,
        # or the reverse. Verified by poisoning: it failed on its own comment.
        for path in sorted(self.WORKFLOW_DIR.glob("*.y*ml")):
            raw = path.read_text(encoding="utf-8")
            code = "\n".join(line for line in raw.splitlines()
                             if not line.lstrip().startswith("#"))
            if "ConfigParser" not in code:
                continue
            writes_back = ".write(" in code
            with self.subTest(workflow=path.name):
                if writes_back:
                    continue
                self.assertIn(
                    "load_repo_config", code,
                    "%s reads configuration with its own parser over a single "
                    "file; a section that lives in the other one resolves to "
                    "the caller's fallback instead of failing" % path.name,
                )

    def test_the_loader_sees_a_section_a_single_file_read_cannot(self):
        # The negative control for the above: prove the two disagree, so the
        # rule is about behaviour rather than style.
        single = configparser.ConfigParser()
        single.read(CONFIG_DIR / "config.ini", encoding="utf-8")
        self.assertFalse(single.has_section("HOTSPOT_RUNTIME"))
        self.assertTrue(load_repo_config().has_section("HOTSPOT_RUNTIME"))


if __name__ == "__main__":
    unittest.main()
