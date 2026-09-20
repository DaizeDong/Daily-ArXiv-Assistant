from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from arxiv_assistant.hotspot.support.dates import is_supported_hotspot_date
from arxiv_assistant.hotspot.support.schema import HotspotItem
from arxiv_assistant.hotspot.support.web_data import write_hotspot_web_data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild hotspot web_data payloads from saved normalized items and reports.")
    parser.add_argument("--output-root", default="out", help="Root output directory that contains hot/ and web_data/.")
    return parser.parse_args()


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_raw_items(path: Path) -> list[HotspotItem]:
    payload = _load_json(path)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a list of hotspot items in {path}")
    return [HotspotItem(**row) for row in payload]


def _extract_zh(data: dict) -> dict[str, dict[str, str]]:
    """Extract _zh translations keyed by headline for later restoration."""
    zh_map: dict[str, dict[str, str]] = {}

    def _save_topic(topic: dict):
        headline = topic.get("headline", "").strip()
        if not headline:
            return
        zh_fields = {k: v for k, v in topic.items() if k.endswith("_zh") and v}
        if zh_fields:
            zh_map[headline] = zh_fields

    for topic in data.get("featured_topics", []):
        _save_topic(topic)
    for sec in data.get("category_sections", []):
        for topic in sec.get("topics", []):
            _save_topic(topic)
    for sec in data.get("long_tail_sections", []):
        for topic in sec.get("topics", []):
            _save_topic(topic)
    for topic in data.get("watchlist", []):
        _save_topic(topic)
    return zh_map


def _merge_zh(data: dict, zh_map: dict[str, dict[str, str]]) -> int:
    """Merge saved _zh translations back into rebuilt data by headline match."""
    restored = 0

    def _restore_topic(topic: dict):
        nonlocal restored
        headline = topic.get("headline", "").strip()
        if headline and headline in zh_map:
            for k, v in zh_map[headline].items():
                existing = topic.get(k)
                # Absent, or present but only repeating its own source. The
                # second case is why this is not a plain "if k not in topic":
                # an earlier failure fallback wrote the English into _zh, and
                # with that key present a genuine translation arriving later had
                # nowhere to land. Measured on 2026-09-20: four fields a run had
                # really translated were dropped here, silently, and the run
                # reported nothing to persist.
                if existing is None or existing == topic.get(k[:-3]):
                    topic[k] = v
            restored += 1

    for topic in data.get("featured_topics", []):
        _restore_topic(topic)
    for sec in data.get("category_sections", []):
        for topic in sec.get("topics", []):
            _restore_topic(topic)
    for sec in data.get("long_tail_sections", []):
        for topic in sec.get("topics", []):
            _restore_topic(topic)
    for topic in data.get("watchlist", []):
        _restore_topic(topic)
    return restored


def rebuild_hotspot_web_data(output_root: str | Path) -> list[str]:
    output_root = Path(output_root)
    hot_root = output_root / "hot"
    reports_root = hot_root / "reports"
    normalized_root = hot_root / "normalized"
    web_root = output_root / "web_data" / "hot"

    if not reports_root.exists():
        return []

    # Save existing _zh translations before rebuilding
    saved_zh: dict[str, dict[str, dict[str, str]]] = {}
    for existing in web_root.glob("202*.json"):
        try:
            old_data = _load_json(existing)
            if isinstance(old_data, dict):
                zh = _extract_zh(old_data)
                if zh:
                    saved_zh[existing.stem] = zh
        except Exception:
            pass

    shutil.rmtree(web_root, ignore_errors=True)
    web_root.mkdir(parents=True, exist_ok=True)

    rebuilt_dates: list[str] = []
    zh_restored_days = 0
    for report_path in sorted(reports_root.glob("*.json")):
        report = _load_json(report_path)
        if not isinstance(report, dict):
            raise ValueError(f"Expected report object in {report_path}")
        date = str(report.get("date") or report_path.stem).strip()
        if not date or not is_supported_hotspot_date(date):
            continue
        normalized_path = normalized_root / f"{date}.json"
        if not normalized_path.exists():
            raise FileNotFoundError(f"Missing normalized hotspot items for {date}: {normalized_path}")
        raw_items = _load_raw_items(normalized_path)
        write_hotspot_web_data(output_root, report, raw_items)
        rebuilt_dates.append(date)

        # Restore _zh translations
        if date in saved_zh:
            web_file = web_root / f"{date}.json"
            if web_file.exists():
                new_data = _load_json(web_file)
                if isinstance(new_data, dict):
                    count = _merge_zh(new_data, saved_zh[date])
                    if count > 0:
                        web_file.write_text(
                            json.dumps(new_data, ensure_ascii=False, indent=None),
                            encoding="utf-8",
                        )
                        zh_restored_days += 1

    if zh_restored_days > 0:
        print(f"Restored _zh translations for {zh_restored_days} day(s).")

    if not rebuilt_dates:
        (web_root / "index.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "latest_date": None,
                    "dates": [],
                    "months": [],
                    "years": [],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    return rebuilt_dates


def main() -> None:
    args = parse_args()
    rebuilt_dates = rebuild_hotspot_web_data(args.output_root)
    print(f"Rebuilt hotspot web_data for {len(rebuilt_dates)} day(s).")


if __name__ == "__main__":
    main()
