"""A day where the model scored nothing must fail, not publish "0 relevant papers" (2026-09-24)."""
import configparser
import pytest

import arxiv_assistant.filters.filter_gpt as fg


def _config(frac=None):
    c = configparser.ConfigParser()
    c["SELECTION"] = {"run_title_filter": "true", "run_abstract_filter": "false", "title_retry": "1"}
    if frac is not None:
        c["SELECTION"]["max_unscored_fraction"] = frac
    c["OUTPUT"] = {"dump_debug_file": "false"}
    return c


def _papers(n):
    from arxiv_assistant.utils.utils import Paper
    return [Paper(authors=["Example Author"], title=f"Paper {i}", abstract="x", arxiv_id=f"2609.{i:05d}")
            for i in range(n)]


def _title_stage(failed):
    def stage(paper_list, *a, **k):
        fg.UNSCORED["title"] += failed
        return paper_list[failed:], {}, 0.0, 0.0, 0, 0
    return stage


@pytest.fixture(autouse=True)
def _offline_backend(monkeypatch):
    from arxiv_assistant.utils import llm_gateway
    monkeypatch.setattr(llm_gateway, "resolve_backend", lambda c: "llmcall")
    monkeypatch.setattr(llm_gateway, "describe_backend", lambda c: "test backend")


def test_every_paper_unscored_fails_the_run(monkeypatch):
    monkeypatch.setattr(fg, "filter_papers_by_title", _title_stage(10))
    with pytest.raises(fg.UnscoredPapersError):
        fg.filter_by_gpt(_papers(10), "", "", "", "", "", _config())


def test_a_few_unscored_papers_still_publish(monkeypatch):
    monkeypatch.setattr(fg, "filter_papers_by_title", _title_stage(1))
    fg.filter_by_gpt(_papers(10), "", "", "", "", "", _config())


def test_the_threshold_is_configurable_and_counts_reset_per_run(monkeypatch):
    monkeypatch.setattr(fg, "filter_papers_by_title", _title_stage(3))
    fg.filter_by_gpt(_papers(10), "", "", "", "", "", _config("0.5"))
    fg.filter_by_gpt(_papers(10), "", "", "", "", "", _config("0.5"))  # 3/10 again, not 6/10
