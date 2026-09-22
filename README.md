# Daily-ArXiv-Assistant

## Introduction

Two daily pipelines, published as one static site. The first scans new arXiv papers and keeps
the ones worth reading; the second gathers AI news from papers, lab blogs, model hubs, GitHub,
Hacker News, X and Reddit, and writes a short digest of what mattered that day.

每日两条流水线，产出同一个静态站点。一条筛选当天的 arXiv 论文，一条汇总来自论文、实验室博客、
模型仓库、GitHub、Hacker News、X 与 Reddit 的 AI 资讯，分享以供交流。

No model API key is needed. Every model call goes through one gateway that prefers the keyless
[llmcall](https://github.com/DaizeDong/llmcall) chain and falls back to the local `claude` CLI.

**Site:** [Daily papers and hotspots](https://daizedong.github.io/Daily-ArXiv-Assistant/)

**Upstream:** [GPT paper assistant](https://github.com/tatsu-lab/gpt_paper_assistant)

## Arrangement

| Path | What it is |
| --- | --- |
| `main.py` | The daily paper run: fetch, filter, render, publish. |
| `arxiv_assistant/apis/` | Paper sources: arXiv, the OAI harvest, a local corpus, Semantic Scholar. |
| `arxiv_assistant/filters/` | Paper scoring. |
| `arxiv_assistant/hotspot/` | The hotspot feed: `sources/` fetch, `support/` shared pieces, the pipeline at the root. |
| `arxiv_assistant/reader/` | The reader model behind the weekly digest. |
| `arxiv_assistant/renderers/` | Markdown and the static site. |
| `arxiv_assistant/utils/` | Gateway, config, health, pricing. |
| `configs/` | `config.ini` (papers) and `hotspot.ini` (feed), read together. `templates/` and `profiles/` ship both halves. |
| `prompts/` | The paper and hotspot prompts. Closer to the behaviour of the filter than the code is. |
| `scripts/` | Backfills, the weekly digest, the archive query, the corpus harvest, site build. |
| `web/`, `site.css` | The published site. |
| `deploy/` | One-command install of the daily job on any Linux host: detects the scheduler, proves a model call and the push path, then schedules. `grokbot_bootstrap.sh` rebuilds the container runner. |
| `out/` | Generated archive. Not on this branch; it lives on `auto_update`. |

## Usage

1. Copy both halves of the config: `configs/templates/config.template.ini` to `configs/config.ini`,
   and `hotspot.template.ini` to `configs/hotspot.ini`. Set `arxiv_category`.
2. Copy `configs/templates/authors.template.txt` to `configs/authors.txt` if you want author matching.
3. Write `prompts/paper/paper_topics.txt`: the topics to keep, and the ones to filter out.
4. Run `main.py` for papers, `scripts/generate_daily_hotspots.py` for the feed.

Results are pushed to the `auto_update` branch; `main` stays code only. On GitHub Actions the
runs are scheduled by `.github/workflows/`.

**Full setup, run modes and backfills:** [docs/SETUP.md](docs/SETUP.md)

## Documentation

| File | What it is |
| --- | --- |
| [docs/SETUP.md](docs/SETUP.md) | Run modes, quickstart, backfilling, the weekly digest, how filtering works. |
| [docs/DAILY_AI_HOTSPOTS.md](docs/DAILY_AI_HOTSPOTS.md) | How the feed picks and ranks stories. |
| [docs/README.md](docs/README.md) | Map of everything else, including what is history. |
| [CHANGELOG.md](CHANGELOG.md) | What changed and why. |

## Acknowledgement

Originally built by Tatsunori Hashimoto, licensed under Apache 2.0.
Thanks to Chenglei Si for testing and benchmarking the GPT filter.
