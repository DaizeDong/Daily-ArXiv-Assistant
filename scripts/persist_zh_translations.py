"""Carry _zh translations into the published data without carrying anything else.

The Pages build translates a snapshot of out/web_data/hot that it took from
auto_update when the run began. By the time it finishes, an hour or more later,
the daily pipeline may have rewritten those same files with fresher hotspot
data. Copying our whole files over theirs would win that race by reverting
their work, and committing ours and rebasing would simply collide on every
shared line.

Neither is necessary, because the two writers do not actually disagree. They
touch disjoint fields: the daily pipeline owns the content, this build owns the
_zh fields beside it. So move only the _zh fields, keyed by headline, onto
whatever the target currently holds. A topic the daily run dropped is not
restored, and a topic it added is simply left untranslated until the next build,
which is the correct answer in both cases.

The extract and merge helpers are the ones rebuild_hotspot_web_data.py already
uses for the same purpose, rather than a second implementation that could drift
from it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.rebuild_hotspot_web_data import _extract_zh, _merge_zh  # noqa: E402


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _layout(text: str) -> tuple[int | None, str]:
    """How the file on disk is laid out: (indent, trailing newline).

    The translator writes its working copies on one line, but the published
    branch keeps these pretty-printed, and that branch is the archive people
    read and diff. Re-serialising a 3758-line file onto one line changes no
    data and destroys every future diff of it, so the target's own layout is
    what gets written back, not this script's preference.
    """
    tail = "\n" if text.endswith("\n") else ""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped), tail
        if line.startswith("\t"):
            return 1, tail
    return None, tail


def persist(source_dir: Path, target_dir: Path) -> tuple[int, int]:
    """Merge _zh from every source day into the matching target day.

    Returns (files_changed, topics_translated).
    """
    files_changed = 0
    topics_total = 0

    for source_path in sorted(source_dir.glob("202*.json")):
        target_path = target_dir / source_path.name
        if not target_path.exists():
            # The target no longer carries this day. Recreating it here would
            # resurrect data something else deliberately removed.
            continue

        source_data = _load(source_path)
        if not isinstance(source_data, dict):
            continue
        zh_map = _extract_zh(source_data)
        if not zh_map:
            continue

        try:
            target_text = target_path.read_text(encoding="utf-8")
            target_data = json.loads(target_text)
        except (OSError, ValueError):
            continue
        if not isinstance(target_data, dict):
            continue

        before = json.dumps(target_data, ensure_ascii=False, sort_keys=True)
        merged = _merge_zh(target_data, zh_map)
        after = json.dumps(target_data, ensure_ascii=False, sort_keys=True)
        if before == after:
            # Already carried these translations. Writing anyway would produce a
            # commit whose diff is empty of meaning but not of noise.
            continue

        indent, tail = _layout(target_text)
        # newline="\n" rather than write_text: on Windows the default translates
        # every \n to \r\n, so a file identical in content would still come back
        # as changed in the working tree and be re-staged on every run.
        with target_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(target_data, ensure_ascii=False, indent=indent) + tail)
        files_changed += 1
        topics_total += merged

    return files_changed, topics_total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path,
                        help="directory of translated web_data/hot json")
    parser.add_argument("--target", required=True, type=Path,
                        help="directory of published web_data/hot json to update")
    args = parser.parse_args()

    for label, path in (("source", args.source), ("target", args.target)):
        if not path.is_dir():
            # A missing directory is not an empty one: silently persisting
            # nothing is how this work came to be redone every build.
            print(f"error: {label} directory does not exist: {path}")
            return 1

    files_changed, topics = persist(args.source, args.target)
    print(f"Persisted translations for {topics} topic(s) across {files_changed} day(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
