"""Issue #36: exposes claim concordance data for DHK-website's research cards.

Per the decisions made when this was scoped: concordance reuses Alexandria's
existing five claim groups verbatim (``web.py``'s ``_GROUPS`` -- consensus,
disagreement, novel, thin, silent) rather than inventing a new taxonomy, and
reaches DHK-website as a static JSON export, not a live API call.

An investigation only produces a concordance entry once it's actually
resolved and published: ``resolution.yaml``'s ``outcome`` must be
``implemented`` (see ``alexandria.resolution``, issue #35) and
``05-analysis/summary.yaml`` must carry a ``groups`` count map. Anything
short of that -- unresolved, morphed, nixed, or resolved without group
counts on file -- produces no entry. This mirrors flow.py's own discipline:
a stated absence, never an invented one.

Nothing here is a live service. ``build_concordance_feed`` is read-only
against research/; writing the feed to disk is a separate, explicit step
(``scripts/export_concordance.py``), same "generated files may be rebuilt,
but nothing writes them automatically" pattern as everything else in this
repo that touches research/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from alexandria.infrastructure.config import Config
from alexandria.infrastructure.research_repo import Investigation, list_investigations
from alexandria.resolution import parse_resolution_yaml

# The same five groups web.py's _GROUPS defines, in the same order, so a
# reader who already knows Alexandria's own claim-landscape view recognizes
# these immediately on DHK-website too.
CONCORDANCE_GROUPS: tuple[str, ...] = ("consensus", "disagreement", "novel", "thin", "silent")


class ConcordanceEntry(BaseModel):
    idea_slug: str
    title: str
    expression: str
    groups: dict[str, int]


class ConcordanceFeed(BaseModel):
    entries: list[ConcordanceEntry]


def _read_groups(investigation_dir: Path) -> dict[str, int] | None:
    summary_path = investigation_dir / "05-analysis" / "summary.yaml"
    if not summary_path.is_file():
        return None
    try:
        raw = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, dict):
        return None
    return {key: _as_count(groups_raw.get(key)) for key in CONCORDANCE_GROUPS}


def _as_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def build_concordance_entry(investigation: Investigation) -> ConcordanceEntry | None:
    """None whenever the investigation doesn't yet qualify -- unresolved,
    not implemented, or resolved without group counts on file. Never raises
    on a malformed resolution.yaml; a broken file just means no entry, since
    a concordance feed is best-effort derived data, not the record itself.
    """
    try:
        resolution = parse_resolution_yaml(investigation.path / "resolution.yaml")
    except Exception:  # noqa: BLE001 -- best-effort derived data, see docstring
        return None
    if resolution is None or resolution.outcome != "implemented" or not resolution.expression:
        return None
    groups = _read_groups(investigation.path)
    if groups is None:
        return None
    return ConcordanceEntry(
        idea_slug=investigation.slug,
        title=investigation.title or investigation.slug,
        expression=resolution.expression,
        groups=groups,
    )


def build_concordance_feed(config: Config) -> ConcordanceFeed:
    entries = [
        entry
        for investigation in list_investigations(config)
        if (entry := build_concordance_entry(investigation)) is not None
    ]
    return ConcordanceFeed(entries=entries)


def feed_to_json(feed: ConcordanceFeed) -> dict[str, Any]:
    return feed.model_dump(mode="json")
