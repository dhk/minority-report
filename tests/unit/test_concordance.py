"""Tests for issue #36: the concordance feed exposed for DHK-website's card."""

from __future__ import annotations

from pathlib import Path

from alexandria.concordance import (
    CONCORDANCE_GROUPS,
    build_concordance_entry,
    build_concordance_feed,
    feed_to_json,
)
from alexandria.infrastructure.config import Config
from alexandria.infrastructure.research_repo import find_investigation

FIXTURES = Path(__file__).parent / "fixtures" / "flow" / "research"


def _config(repo_root: Path) -> Config:
    return Config(
        repo_root=repo_root,
        repo_root_source="test fixture",
        data_dir=repo_root / "_data",
        data_dir_source="test fixture",
    )


def test_shipped_investigation_produces_a_concordance_entry() -> None:
    config = _config(FIXTURES.parent)
    investigation = find_investigation(config, "idea-shipped")
    assert investigation is not None
    entry = build_concordance_entry(investigation)
    assert entry is not None
    assert entry.idea_slug == "idea-shipped"
    assert entry.expression == "08-published/recommendation.md"
    assert entry.groups == {
        "consensus": 4,
        "disagreement": 2,
        "novel": 1,
        "thin": 1,
        "silent": 0,
    }
    assert set(entry.groups) == set(CONCORDANCE_GROUPS)


def test_stalled_investigation_produces_no_entry() -> None:
    config = _config(FIXTURES.parent)
    investigation = find_investigation(config, "idea-stalled")
    assert investigation is not None
    assert build_concordance_entry(investigation) is None


def test_feed_only_contains_qualifying_entries() -> None:
    config = _config(FIXTURES.parent)
    feed = build_concordance_feed(config)
    slugs = {entry.idea_slug for entry in feed.entries}
    assert slugs == {"idea-shipped"}


def test_feed_to_json_is_json_safe() -> None:
    config = _config(FIXTURES.parent)
    feed = build_concordance_feed(config)
    payload = feed_to_json(feed)
    import json

    json.dumps(payload)  # raises if anything isn't JSON-safe
    assert payload["entries"][0]["idea_slug"] == "idea-shipped"


def test_resolved_without_group_counts_produces_no_entry(tmp_path: Path) -> None:
    investigation_dir = tmp_path / "research" / "resolved-no-groups"
    (investigation_dir / "05-analysis").mkdir(parents=True)
    (investigation_dir / "topic.yaml").write_text("title: No groups yet\n", encoding="utf-8")
    (investigation_dir / "resolution.yaml").write_text(
        "outcome: implemented\nexpression: 08-published/piece.md\n", encoding="utf-8"
    )
    (investigation_dir / "05-analysis" / "summary.yaml").write_text(
        "headline: Results without a groups map\n", encoding="utf-8"
    )
    config = _config(tmp_path)
    investigation = find_investigation(config, "resolved-no-groups")
    assert investigation is not None
    assert build_concordance_entry(investigation) is None


def test_morphed_investigation_produces_no_entry(tmp_path: Path) -> None:
    investigation_dir = tmp_path / "research" / "morphed-idea"
    (investigation_dir / "05-analysis").mkdir(parents=True)
    (investigation_dir / "topic.yaml").write_text(
        "title: Became something else\n", encoding="utf-8"
    )
    (investigation_dir / "resolution.yaml").write_text(
        "outcome: morphed\nexpression: research/successor\n", encoding="utf-8"
    )
    (investigation_dir / "05-analysis" / "summary.yaml").write_text(
        "groups: {consensus: 3, disagreement: 0, novel: 0, thin: 0, silent: 0}\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    investigation = find_investigation(config, "morphed-idea")
    assert investigation is not None
    # Concordance is about a published piece; a morphed idea has no expression
    # that's itself a finished piece worth citing, only a forward pointer.
    assert build_concordance_entry(investigation) is None


def test_missing_group_keys_default_to_zero(tmp_path: Path) -> None:
    investigation_dir = tmp_path / "research" / "partial-groups"
    (investigation_dir / "05-analysis").mkdir(parents=True)
    (investigation_dir / "topic.yaml").write_text("title: Partial\n", encoding="utf-8")
    (investigation_dir / "resolution.yaml").write_text(
        "outcome: implemented\nexpression: 08-published/piece.md\n", encoding="utf-8"
    )
    (investigation_dir / "05-analysis" / "summary.yaml").write_text(
        "groups: {consensus: 5}\n", encoding="utf-8"
    )
    config = _config(tmp_path)
    investigation = find_investigation(config, "partial-groups")
    assert investigation is not None
    entry = build_concordance_entry(investigation)
    assert entry is not None
    assert entry.groups == {
        "consensus": 5,
        "disagreement": 0,
        "novel": 0,
        "thin": 0,
        "silent": 0,
    }
