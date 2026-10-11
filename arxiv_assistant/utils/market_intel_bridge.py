"""Bridge that lets the agent scout REUSE the ``market-intel`` skill's curated
source matrix at runtime.

The ``market-intel`` skill ships per-domain "shards" (``reference/domains/*.md``)
whose source TABLES (``| source | route | capability | detect | note |``) are a
curated, periodically-refreshed map of where to look + how to get each source.
By loading the relevant shards here and injecting them into the scout's research
prompt, the scout's discovery breadth tracks the skill -- a skill refresh
auto-improves the scout, with NO duplication of the venue list in our code.

Pure stdlib, no network, no side effects. Every failure degrades to ``None`` so
the caller transparently falls back to its built-in venue list.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

# A shard that MUST be present for a candidate dir to count as the market-intel
# domains directory (the academic/frontier-research domain we primarily reuse).
_ANCHOR_SHARD = "frontier-research.md"

_DEFAULT_DOMAINS: tuple[str, ...] = ("frontier-research", "x-twitter")


def _candidate_domains_dirs(explicit: str | None) -> list[Path]:
    """Ordered candidate ROOTS to probe; each is expanded to a domains dir."""
    roots: list[str] = []
    if explicit:
        roots.append(explicit)
    env = os.getenv("MARKET_INTEL_SKILL_DIR")
    if env:
        roots.append(env)
    home = Path.home()
    roots.append(str(home / ".claude" / "skills" / "market-intel"))
    return [Path(r).expanduser() for r in roots if r]


def _resolve_domains_dir(candidate: Path) -> Path | None:
    """Map a root/skill/repo/domains path to its ``reference/domains`` dir.

    A user may point us at the domains dir directly, the skill root, or the
    plugin/repo root (which nests the skill under ``skills/market-intel``). Try
    each shape and return the first that actually holds the anchor shard.
    """
    shapes = (
        candidate,
        candidate / "reference" / "domains",
        candidate / "skills" / "market-intel" / "reference" / "domains",
    )
    for shape in shapes:
        try:
            if (shape / _ANCHOR_SHARD).is_file():
                return shape
        except OSError:  # unreadable path component -> treat as absent
            continue
    return None


def find_skill_domains_dir(*, explicit: str | None = None) -> Path | None:
    """Locate the market-intel skill's ``reference/domains`` dir, or ``None``.

    Probe order: ``explicit`` arg -> ``MARKET_INTEL_SKILL_DIR`` env ->
    installed skill (~/.claude/skills) -> repo checkout (~/CodesSelf). Returns
    the first directory that contains ``frontier-research.md``.
    """
    for candidate in _candidate_domains_dirs(explicit):
        resolved = _resolve_domains_dir(candidate)
        if resolved is not None:
            return resolved
    return None


def _extract_source_section(text: str) -> str | None:
    """Pull the source TABLE (+ a ``**Default pick:**`` line) from a shard.

    Robust to format tweaks: if no ``| source | route |`` table is found, fall
    back to the shard's first ~40 non-empty lines so the agent still gets the
    venue guidance. Returns ``None`` only for empty input.
    """
    lines = text.splitlines()
    if not any(ln.strip() for ln in lines):
        return None

    # Find the table header row: a pipe row mentioning both "source" and "route".
    header_idx = -1
    for i, ln in enumerate(lines):
        low = ln.lower()
        if ln.lstrip().startswith("|") and "source" in low and "route" in low:
            header_idx = i
            break

    out: list[str] = []
    if header_idx != -1:
        # Capture the contiguous pipe-table block starting at the header.
        j = header_idx
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            out.append(lines[j].rstrip())
            j += 1
        # Append the Default-pick guidance if present anywhere in the shard.
        for k, ln in enumerate(lines):
            if ln.lstrip().lower().startswith("**default pick"):
                blk = [lines[k].rstrip()]
                m = k + 1
                while m < len(lines) and lines[m].strip():
                    blk.append(lines[m].rstrip())
                    m += 1
                out.append("")
                out.extend(blk)
                break
    else:
        # Fallback: first ~40 non-empty lines (skip the leading H1 title).
        for ln in lines:
            if ln.strip():
                if ln.lstrip().startswith("# "):
                    continue
                out.append(ln.rstrip())
            if len(out) >= 40:
                break

    block = "\n".join(out).strip()
    return block or None


def load_source_guidance(
    domains: Sequence[str] = _DEFAULT_DOMAINS,
    *,
    explicit_dir: str | None = None,
    max_chars: int = 4000,
) -> str | None:
    """Return a compact, prompt-ready source matrix reused from the skill shards.

    Concatenates the source-table section of each requested domain shard that
    exists. Returns ``None`` if the skill cannot be located or no shard is
    readable (the caller then falls back to its built-in venue list). Never
    raises -- any error degrades to ``None``.
    """
    try:
        domains_dir = find_skill_domains_dir(explicit=explicit_dir)
        if domains_dir is None:
            return None
        blocks: list[str] = []
        for domain in domains:
            shard = domains_dir / f"{domain}.md"
            if not shard.is_file():
                continue
            section = _extract_source_section(shard.read_text(encoding="utf-8"))
            if section:
                blocks.append(f"[{domain}]\n{section}")
        if not blocks:
            return None
        guidance = "\n\n".join(blocks).strip()
        if len(guidance) > max_chars:
            guidance = guidance[:max_chars].rstrip() + "\n... (truncated)"
        return guidance or None
    except Exception:  # noqa: BLE001 -- reuse is best-effort; never break the scout
        return None
