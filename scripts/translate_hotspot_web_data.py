"""Translate hotspot web data JSON files to Chinese using LLM."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from arxiv_assistant.utils.local_env import load_local_env

# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------

def _chat(model: str, messages: list[dict], temperature: float = 0.1) -> str:
    """Translate one batch through the gateway (keyless llmcall chain by default)."""
    from arxiv_assistant.utils import llm_gateway

    load_local_env()
    prompt = "\n\n---\n\n".join(
        str(message.get("content", "")) for message in messages if message.get("content")
    )
    # This is the budget for the WHOLE chain, not for one provider: llmcall hands
    # each attempt whatever is left and skips a provider that would get less than
    # it can use. At 180s that was self-defeating. This repo measured its own
    # answers at a median of 134s and a p95 of 428s, so the first provider could
    # spend the entire budget and the next leg would be told it had 18 seconds --
    # the error that filled the logs while the published site went stale. A budget
    # has to fit one slow answer AND a fallback, or the fallback is decorative.
    return llm_gateway.call(prompt, timeout_s=CHAIN_BUDGET_S).text


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

#: Budget handed to llmcall for one model call. It covers the whole provider
#: chain, not each provider in it. See _chat() for why 180 was too small.
CHAIN_BUDGET_S = 900

#: Minutes this script may spend before it stops starting new days. The job
#: around it is killed at 360 minutes, and a killed job skips every step
#: after this one -- including the step that pushes what was translated. Two
#: consecutive runs were cancelled that way, each having translated about
#: twenty days and saved none of them. Finishing early with a partial result
#: that is kept beats finishing late with a complete one that is discarded.
DEADLINE_MINUTES = int(os.environ.get("TRANSLATE_DEADLINE_MINUTES", "270"))

#: Batches sent at once. The batches of one day are disjoint slices of the
#: same list, so nothing is shared but the results array each writes its own
#: indices into. Sequential was costing the whole wall clock: a day is about
#: six calls and this repository's own measurement puts a call at a median of
#: 134s, so a day took a quarter of an hour and 160 days of backlog could
#: never fit in a six hour job.
BATCH_CONCURRENCY = int(os.environ.get("TRANSLATE_CONCURRENCY", "6"))

#: Attempts per batch. Each one may cost CHAIN_BUDGET_S, so this bounds the
#: worst case for a single stubborn batch at half an hour rather than three
#: quarters of one.
BATCH_ATTEMPTS = 2

SYSTEM_PROMPT = """\
You are a professional translator. Translate the following JSON array of English text strings into Chinese.
Rules:
- Return a JSON array of the same length, each element being the Chinese translation.
- Keep proper nouns (company names, product names, people names) in their original form or use their well-known Chinese translation.
- Technical terms can keep English if no standard Chinese translation exists.
- Keep translations concise and natural.
- If a string is already in Chinese, return it unchanged.
- If a string is empty, return empty string.
- Return ONLY the JSON array, no markdown fences, no explanation."""


def _is_chinese(text: str) -> bool:
    """Check if text is predominantly Chinese."""
    if not text.strip():
        return True
    chinese_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return chinese_chars > len(text.strip()) * 0.3


def batch_translate(texts: list[str], model: str, batch_size: int = 40) -> list[str]:
    """Translate a list of texts to Chinese, skipping already-Chinese ones."""
    results = [""] * len(texts)
    # Mark which ones need translation
    to_translate: list[tuple[int, str]] = []
    for i, t in enumerate(texts):
        if not t.strip() or _is_chinese(t):
            results[i] = t
        else:
            to_translate.append((i, t))

    if not to_translate:
        return results

    batches = [to_translate[i : i + batch_size]
               for i in range(0, len(to_translate), batch_size)]
    total = len(batches)

    def _run(job):
        number, batch = job
        batch_texts = [t for _, t in batch]
        user_msg = json.dumps(batch_texts, ensure_ascii=False)

        # Two attempts, not three. Each one may run for CHAIN_BUDGET_S, and a
        # batch the four-provider chain could not answer twice will not answer
        # on a third try -- it will only cost another quarter of an hour.
        for attempt in range(BATCH_ATTEMPTS):
            try:
                raw = _chat(model, [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ])
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```\w*\n?", "", raw)
                    raw = re.sub(r"\n?```$", "", raw)
                translated = json.loads(raw)
                if len(translated) != len(batch_texts):
                    raise ValueError(
                        f"Expected {len(batch_texts)} translations, got {len(translated)}")
                return number, [(idx, zh) for (idx, _), zh in zip(batch, translated)], True
            except Exception as e:  # noqa: BLE001 - any failure falls back below
                print(f"  Batch {number} attempt {attempt + 1} failed: {e}")
                if attempt < BATCH_ATTEMPTS - 1:
                    time.sleep(2)
        # None, not the English text. Writing the source string into the _zh
        # field looks like a completed translation to the next run, which skips
        # a day on exactly that signal -- so one failed batch used to freeze its
        # day as permanently untranslated. Recording nothing leaves the field
        # absent, the site falls back to English for it as it always has, and
        # the next run picks the day up again.
        return number, [(idx, None) for idx, _ in batch], False

    jobs = list(enumerate(batches, start=1))
    workers = max(1, min(BATCH_CONCURRENCY, total))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for number, pairs, ok in pool.map(_run, jobs):
            for idx, value in pairs:
                results[idx] = value
            state = "" if ok else " (no answer; left for the next run)"
            print(f"  Translated batch {number}/{total} ({len(pairs)} items){state}")

    return results


# ---------------------------------------------------------------------------
# JSON traversal
# ---------------------------------------------------------------------------

# Fields to translate with their paths
TOPIC_TEXT_FIELDS = ["headline", "summary_short", "why_it_matters", "category"]
TOPIC_LIST_FIELDS = ["key_takeaways"]
EVIDENCE_FIELDS = ["title"]
ITEM_TEXT_FIELDS = ["title", "summary_short", "spotlight_comment"]
SECTION_TEXT_FIELDS = ["label", "description"]


def collect_and_translate(data: dict, model: str) -> dict:
    """Add _zh fields to all translatable text in the payload."""
    all_texts: list[str] = []
    registry: list[tuple[dict, str, int]] = []  # (obj, field_name, index_in_all_texts)

    def register(obj: dict, field: str):
        # "Already translated" has to mean the same thing here as it does in
        # _has_zh_fields, or the two disagree and the disagreement is a silent
        # infinite no-op: that function picks a day BECAUSE a _zh merely repeats
        # its English source, and this one then skips every field BECAUSE a _zh
        # is present. The day is reselected, rewritten unchanged, and reselected
        # again on every future run, translating nothing.
        zh_key = f"{field}_zh"
        val = obj.get(field, "")
        existing = obj.get(zh_key)
        if existing and existing != val:
            return  # already translated
        if isinstance(val, str):
            idx = len(all_texts)
            all_texts.append(val)
            registry.append((obj, field, idx))

    def register_list(obj: dict, field: str):
        # Same rule as register(): a _zh list that just repeats its source is
        # not a translation, and treating it as one strands the day forever.
        zh_key = f"{field}_zh"
        val = obj.get(field, [])
        existing = obj.get(zh_key)
        if existing and existing != val:
            return  # already translated
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    idx = len(all_texts)
                    all_texts.append(item)
                    registry.append((obj, f"{field}[]", idx))

    # Collect from featured_topics
    for topic in data.get("featured_topics", []):
        for f in TOPIC_TEXT_FIELDS:
            register(topic, f)
        register_list(topic, "key_takeaways")
        for ev in topic.get("evidence", []):
            for f in EVIDENCE_FIELDS:
                register(ev, f)

    # category_sections + long_tail_sections
    for section_list_key in ("category_sections", "long_tail_sections"):
        for sec in data.get(section_list_key, []):
            register(sec, "category")
            for topic in sec.get("topics", []):
                for f in TOPIC_TEXT_FIELDS:
                    register(topic, f)
                register_list(topic, "key_takeaways")
                for ev in topic.get("evidence", []):
                    for f in EVIDENCE_FIELDS:
                        register(ev, f)

    # watchlist
    for topic in data.get("watchlist", []):
        for f in TOPIC_TEXT_FIELDS:
            register(topic, f)
        register_list(topic, "key_takeaways")
        for ev in topic.get("evidence", []):
            for f in EVIDENCE_FIELDS:
                register(ev, f)

    # resurgence
    for entry in data.get("resurgence", []):
        register(entry, "headline")

    # source_sections + paper_spotlight
    for section_list_key in ("source_sections", "paper_spotlight"):
        for sec in data.get(section_list_key, []):
            for f in SECTION_TEXT_FIELDS:
                register(sec, f)
            for item in sec.get("items", []):
                for f in ITEM_TEXT_FIELDS:
                    register(item, f)

    # meta summary
    if "meta" in data and "summary" in data["meta"]:
        register(data["meta"], "summary")

    print(f"  Collected {len(all_texts)} text fields to translate")

    # Translate all at once
    translated = batch_translate(all_texts, model)

    # Write back as _zh fields
    # A list field is all or nothing: a _zh list with a hole in it would be
    # rendered against the wrong source items.
    lists: dict[tuple[int, str], list] = {}
    holders: dict[tuple[int, str], dict] = {}
    broken: set[tuple[int, str]] = set()

    for obj, field, idx in registry:
        zh = translated[idx]
        if field.endswith("[]"):
            key = (id(obj), field[:-2])
            holders[key] = obj
            if zh is None:
                broken.add(key)
                continue
            lists.setdefault(key, []).append(zh)
        elif zh is not None:
            obj[f"{field}_zh"] = zh

    for key, values in lists.items():
        if key in broken:
            continue
        holders[key][f"{key[1]}_zh"] = values

    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _has_zh_fields(data: dict) -> bool:
    """Has this payload really been translated?

    Presence of the field is not enough. An earlier fallback wrote the English
    source into _zh when a batch failed, which reads as "translated" and made
    the day permanently ineligible for another attempt. Treating a _zh that
    merely repeats its source as untranslated lets those days heal by
    themselves on the next run.
    """
    topics = data.get("featured_topics", [])
    if not topics:
        return False
    for topic in topics:
        zh = topic.get("headline_zh")
        if zh is None or zh == topic.get("headline"):
            return False
    return True


def translate_file(json_path: Path, model: str) -> bool:
    """Translate a daily hotspot file. Returns True if any translation was done."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if _has_zh_fields(data):
        print(f"Skipping {json_path.name} (already translated)")
        return False
    print(f"Translating {json_path.name}...")
    data = collect_and_translate(data, model)
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=None), encoding="utf-8")
    print(f"  Done: {json_path.name}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Translate a specific date (e.g. 2026-04-03)")
    parser.add_argument("--model", default="gpt-5.4", help="LLM model to use")
    parser.add_argument("--batch-size", type=int, default=40, help="Texts per LLM call")
    args = parser.parse_args()

    out_dir = REPO_ROOT / "out" / "web_data" / "hot"
    web_dir = REPO_ROOT / "web" / "public" / "web_data" / "hot"

    if args.date:
        targets = [out_dir / f"{args.date}.json"]
    else:
        targets = sorted(out_dir.glob("202*.json"))

    deadline = time.monotonic() + DEADLINE_MINUTES * 60
    done = 0
    for position, path in enumerate(targets):
        if not path.exists():
            print(f"Skipping {path} (not found)")
            continue
        if time.monotonic() >= deadline:
            # Checked between days, never inside one: a half-translated day
            # written back would look translated to the next run, which skips on
            # exactly that signal, and the untranslated half would never be
            # revisited.
            remaining = len(targets) - position
            print(f"Stopping after {DEADLINE_MINUTES} minutes with {remaining} "
                  f"day(s) left. Translated {done} day(s) this run; they are "
                  f"persisted, and the next run resumes from {path.stem}.")
            break
        translate_file(path, args.model)
        done += 1

    # Copy to web/public
    import shutil
    if web_dir.exists():
        for path in targets:
            if path.exists():
                dest = web_dir / path.name
                shutil.copy2(path, dest)
                print(f"  Copied to {dest}")

    print("Translation complete!")


if __name__ == "__main__":
    main()
