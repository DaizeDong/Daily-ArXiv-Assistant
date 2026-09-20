"""Check the translations that the pipeline actually produces.

This script used to scan out/hot/web_data for `<day>_zh.json` companion files.
Nothing has written those for as long as the branch records: there are zero of
them on auto_update. The translator writes _zh fields inline into
out/web_data/hot/<day>.json instead. So the loop iterated an empty directory,
found no problems, and printed that all assets were present and non-trivial --
a green that meant "nothing was examined", which is indistinguishable from a
green that means "nothing is wrong" unless the check says which it is.

What it checks now, and what it deliberately does not:

- A missing or empty directory is a failure, not an empty pass. That is the
  bug above, and it must not be able to recur quietly.
- A _zh list whose length differs from its source list is a failure: it renders
  against the wrong items, which is worse than no translation at all.
- No day carrying any translation at all is a failure. Days through 2026-04-04
  are translated on the branch, so an empty result means something upstream
  stopped working rather than that there was nothing to do.
- Untranslated days are NOT a failure. The translator stops on a deadline so
  that what it did finish can be saved, so a partial result is the normal
  outcome of a healthy run.
- A _zh that merely repeats its English source is reported, not failed. The
  model does sometimes echo a string, and the translator now treats such a
  field as untranslated and tries it again, so this heals on its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DATA = REPO_ROOT / "out" / "web_data" / "hot"

TEXT_FIELDS = ("headline", "summary", "why_it_matters")


def _topics(data: dict):
    yield from data.get("featured_topics", [])
    for section in data.get("category_sections", []):
        yield from section.get("topics", [])
    for section in data.get("long_tail_sections", []):
        yield from section.get("topics", [])
    yield from data.get("watchlist", [])


def main() -> int:
    if not WEB_DATA.is_dir():
        print(f"error: {WEB_DATA} does not exist. Nothing was examined, which is "
              f"not the same as nothing being wrong.")
        return 1

    days = sorted(WEB_DATA.glob("202*.json"))
    if not days:
        print(f"error: no daily payloads in {WEB_DATA}. Nothing was examined.")
        return 1

    mismatched: list[str] = []
    echoed = 0
    translated_days = 0

    for path in days:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: {path.name} is unreadable: {exc}")
            return 1
        if not isinstance(data, dict):
            continue

        day_has_translation = False
        for topic in _topics(data):
            for field in TEXT_FIELDS:
                zh = topic.get(f"{field}_zh")
                if zh is None:
                    continue
                source = topic.get(field)
                if isinstance(zh, list) or isinstance(source, list):
                    if not isinstance(zh, list) or not isinstance(source, list) \
                            or len(zh) != len(source):
                        mismatched.append(f"{path.name}: {field}_zh")
                        continue
                    day_has_translation = True
                    continue
                if zh == source:
                    echoed += 1
                    continue
                day_has_translation = True
        if day_has_translation:
            translated_days += 1

    if mismatched:
        print("A _zh list does not line up with its source, so it would render "
              "against the wrong items:", *mismatched, sep="\n  ")
        return 1

    if translated_days == 0:
        print(f"error: none of the {len(days)} day(s) carry a translation. Days "
              f"through 2026-04-04 are translated on the branch, so this means "
              f"something stopped working rather than that there was no work.")
        return 1

    print(f"{translated_days}/{len(days)} day(s) carry translations; "
          f"{len(days) - translated_days} still to do.")
    if echoed:
        print(f"{echoed} field(s) came back identical to their English source. "
              f"These count as untranslated and will be retried.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
