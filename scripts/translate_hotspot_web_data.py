"""Translate hotspot web data JSON files to Chinese using LLM."""

from __future__ import annotations

import argparse
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

    # Batch
    for start in range(0, len(to_translate), batch_size):
        batch = to_translate[start : start + batch_size]
        batch_texts = [t for _, t in batch]
        user_msg = json.dumps(batch_texts, ensure_ascii=False)

        # Two attempts, not three. Each one may now run for CHAIN_BUDGET_S, and a
        # batch the four-provider chain could not answer twice will not answer on
        # a third try -- it will only cost another quarter of an hour.
        for attempt in range(BATCH_ATTEMPTS):
            try:
                raw = _chat(model, [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ])
                # Parse JSON from response
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```\w*\n?", "", raw)
                    raw = re.sub(r"\n?```$", "", raw)
                translated = json.loads(raw)
                if len(translated) != len(batch_texts):
                    raise ValueError(f"Expected {len(batch_texts)} translations, got {len(translated)}")
                for (idx, _), zh in zip(batch, translated):
                    results[idx] = zh
                break
            except Exception as e:
                print(f"  Batch {start//batch_size + 1} attempt {attempt+1} failed: {e}")
                if attempt < BATCH_ATTEMPTS - 1:
                    time.sleep(2)
                else:
                    # Fallback: keep originals
                    for idx, orig in batch:
                        results[idx] = orig

        print(f"  Translated batch {start//batch_size + 1}/{(len(to_translate) + batch_size - 1) // batch_size} ({len(batch)} items)")

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
        zh_key = f"{field}_zh"
        if zh_key in obj and obj[zh_key]:
            return  # already translated
        val = obj.get(field, "")
        if isinstance(val, str):
            idx = len(all_texts)
            all_texts.append(val)
            registry.append((obj, field, idx))

    def register_list(obj: dict, field: str):
        zh_key = f"{field}_zh"
        if zh_key in obj and obj[zh_key]:
            return  # already translated
        val = obj.get(field, [])
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
    key_takeaways_map: dict[int, list[str]] = {}  # obj_id -> list of translated items
    for obj, field, idx in registry:
        zh = translated[idx]
        if field.endswith("[]"):
            base = field[:-2]
            obj_id = id(obj)
            if obj_id not in key_takeaways_map:
                key_takeaways_map[obj_id] = []
            key_takeaways_map[obj_id].append(zh)
            obj[f"{base}_zh"] = key_takeaways_map[obj_id]
        else:
            obj[f"{field}_zh"] = zh

    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _has_zh_fields(data: dict) -> bool:
    """Quick check: does the payload already have _zh fields on featured_topics?"""
    for topic in data.get("featured_topics", []):
        if "headline_zh" not in topic:
            return False
    return bool(data.get("featured_topics"))


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

    for path in targets:
        if not path.exists():
            print(f"Skipping {path} (not found)")
            continue
        translate_file(path, args.model)

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
