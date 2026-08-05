"""Tests for issue #35's resolution taxonomy (src/alexandria/resolution.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alexandria.resolution import (
    RESOLUTION_FILENAME,
    Resolution,
    ResolutionError,
    draft_resolution,
    parse_resolution_yaml,
)


def test_implemented_does_not_require_expression() -> None:
    resolution = draft_resolution(outcome="implemented")
    assert resolution.outcome == "implemented"
    assert resolution.expression is None


def test_nixed_does_not_require_expression() -> None:
    resolution = draft_resolution(outcome="nixed", rationale="Superseded by a better idea.")
    assert resolution.outcome == "nixed"


def test_morphed_without_expression_is_rejected() -> None:
    with pytest.raises(ResolutionError, match="expression"):
        draft_resolution(outcome="morphed")


def test_morphed_with_expression_is_accepted() -> None:
    resolution = draft_resolution(outcome="morphed", expression="research/successor-idea")
    assert resolution.outcome == "morphed"
    assert resolution.expression == "research/successor-idea"


def test_unknown_outcome_is_rejected() -> None:
    with pytest.raises(ResolutionError):
        draft_resolution(outcome="back-burnered")


def test_to_yaml_round_trips_through_parse(tmp_path: Path) -> None:
    resolution = draft_resolution(
        outcome="morphed",
        expression="research/successor",
        decided_by="operator",
        decided_at="2026-08-05",
        rationale="Became a different idea entirely.",
    )
    path = tmp_path / RESOLUTION_FILENAME
    path.write_text(resolution.to_yaml(), encoding="utf-8")
    reloaded = parse_resolution_yaml(path)
    assert reloaded == resolution


def test_parse_returns_none_when_absent(tmp_path: Path) -> None:
    assert parse_resolution_yaml(tmp_path / RESOLUTION_FILENAME) is None


def test_parse_raises_on_morphed_without_expression_on_disk(tmp_path: Path) -> None:
    path = tmp_path / RESOLUTION_FILENAME
    path.write_text("outcome: morphed\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="expression"):
        parse_resolution_yaml(path)


def test_parse_raises_on_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / RESOLUTION_FILENAME
    path.write_text("outcome: [unclosed\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="not valid YAML"):
        parse_resolution_yaml(path)


def test_parse_raises_on_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / RESOLUTION_FILENAME
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="mapping"):
        parse_resolution_yaml(path)


def test_valid_resolution_parses_from_disk(tmp_path: Path) -> None:
    path = tmp_path / RESOLUTION_FILENAME
    path.write_text("outcome: implemented\nexpression: 08-published/piece.md\n", encoding="utf-8")
    resolution = parse_resolution_yaml(path)
    assert isinstance(resolution, Resolution)
    assert resolution.outcome == "implemented"
    assert resolution.expression == "08-published/piece.md"
