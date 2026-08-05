"""Tests for the RFC-0007 idea-to-expression flow document assembly (flow.py).

Exercises the two fixtures RFC-0007's own acceptance criteria call for
(SS09): one resolved idea with a failed call and a superseded stage
("idea-shipped"), and one idea that stopped after stage 03 ("idea-stalled").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alexandria.flow import build_flow_document, flow_document_json
from alexandria.infrastructure.config import Config

FIXTURES = Path(__file__).parent / "fixtures" / "flow"


def _config(repo_root: Path) -> Config:
    return Config(
        repo_root=repo_root,
        repo_root_source="test fixture",
        data_dir=repo_root / "_data",
        data_dir_source="test fixture",
    )


@pytest.fixture
def config() -> Config:
    return _config(FIXTURES)


def test_missing_slug_returns_none(config: Config) -> None:
    assert build_flow_document(config, "does-not-exist") is None


# --- idea-shipped: AC-01, AC-02(data shape), AC-03, AC-04, AC-05, AC-10, AC-11 ---


def test_shipped_all_six_stages_present_in_fixed_order(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    assert [s.key for s in doc.stages] == [
        "idea",
        "brief",
        "run",
        "results",
        "synthesis",
        "resolution",
    ]
    assert all(s.state == "present" for s in doc.stages)


def test_shipped_every_stage_has_exactly_four_lanes(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    for stage in doc.stages:
        assert len(stage.lanes) == 4
        for lane in stage.lanes:
            assert lane.summary  # never empty/None -- "Not recorded" stands in


def test_shipped_run_stage_surfaces_failure_at_level_0(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    run_stage = next(s for s in doc.stages if s.key == "run")
    assert run_stage.failed_calls == 1
    assert "1 failed" in run_stage.headline
    assert any("failed" in m for m in run_stage.meta)


def test_shipped_run_stage_records_superseded_count(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    run_stage = next(s for s in doc.stages if s.key == "run")
    assert run_stage.superseded_count == 1


def test_shipped_excerpts_never_exceed_available_paragraphs(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    for stage in doc.stages:
        if stage.excerpt is None:
            continue
        shown, total = stage.excerpt.shown_of_total
        assert shown <= total
        assert len(stage.excerpt.paragraphs) == shown


def test_shipped_resolution_outcome_and_forward_pointer(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    assert doc.resolution is not None
    assert doc.resolution.outcome == "implemented"
    assert doc.resolution.expression == "08-published/recommendation.md"
    resolution_stage = next(s for s in doc.stages if s.key == "resolution")
    assert resolution_stage.headline == "implemented"
    expression_lane = next(l for l in resolution_stage.lanes if l.label == "Expression")
    assert "recommendation.md" in expression_lane.summary


def test_shipped_resolution_excerpt_reads_the_published_piece(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    resolution_stage = next(s for s in doc.stages if s.key == "resolution")
    assert resolution_stage.excerpt is not None
    assert "recommendation.md" in resolution_stage.excerpt.artifact_path


# --- idea-stalled: AC-08 (not_reached), AC-09 (distinct from abandoned) ---


def test_stalled_stops_after_run_stage(config: Config) -> None:
    doc = build_flow_document(config, "idea-stalled")
    assert doc is not None
    states = {s.key: s.state for s in doc.stages}
    assert states["idea"] == "present"
    assert states["brief"] == "present"
    assert states["run"] == "present"
    assert states["results"] == "not_reached"
    assert states["synthesis"] == "not_reached"
    assert states["resolution"] == "not_reached"


def test_stalled_not_reached_stages_still_render_four_lanes(config: Config) -> None:
    doc = build_flow_document(config, "idea-stalled")
    assert doc is not None
    results_stage = next(s for s in doc.stages if s.key == "results")
    assert len(results_stage.lanes) == 4
    assert all(lane.summary == "Not recorded" for lane in results_stage.lanes)


def test_stalled_run_stage_has_no_failures(config: Config) -> None:
    doc = build_flow_document(config, "idea-stalled")
    assert doc is not None
    run_stage = next(s for s in doc.stages if s.key == "run")
    assert run_stage.failed_calls == 0
    assert run_stage.state == "present"


# --- abandoned vs. not_reached distinction (AC-09), synthesised via a third,
# in-memory-only case rather than a third fixture directory ---


def test_reached_but_empty_stage_reads_as_abandoned_not_not_reached(
    tmp_path: Path,
) -> None:
    investigation = tmp_path / "research" / "idea-abandoned-middle"
    (investigation / "00-topic").mkdir(parents=True)
    (investigation / "01-brief").mkdir(parents=True)
    # 02/03/04 deliberately produce nothing...
    (investigation / "05-analysis").mkdir(parents=True)  # ...but analysis exists.
    (investigation / "topic.yaml").write_text("title: Abandoned in the middle\n")
    (investigation / "01-brief" / "brief.md").write_text("Task\nSomething.\n")
    (investigation / "05-analysis" / "summary.yaml").write_text(
        "headline: Results despite no run artifact on file\n"
    )
    config = _config(tmp_path)
    doc = build_flow_document(config, "idea-abandoned-middle")
    assert doc is not None
    states = {s.key: s.state for s in doc.stages}
    assert states["run"] == "abandoned"  # reached, empty, but results exists after it
    assert states["results"] == "present"


# --- unresolved (reached synthesis, no resolution.yaml) ---


def test_synthesis_without_resolution_reads_as_unresolved(tmp_path: Path) -> None:
    investigation = tmp_path / "research" / "idea-unresolved"
    (investigation / "06-synthesis").mkdir(parents=True)
    (investigation / "topic.yaml").write_text("title: Still open\n")
    (investigation / "06-synthesis" / "summary.yaml").write_text("headline: Findings exist\n")
    config = _config(tmp_path)
    doc = build_flow_document(config, "idea-unresolved")
    assert doc is not None
    resolution_stage = next(s for s in doc.stages if s.key == "resolution")
    assert resolution_stage.state == "present"
    assert resolution_stage.headline == "Unresolved"
    outcome_lane = next(l for l in resolution_stage.lanes if l.label == "Outcome")
    assert outcome_lane.summary == "UNRESOLVED"


# --- JSON wire shape (what the client-side flow actually consumes) ---


def test_flow_document_json_is_json_safe_and_has_six_stages(config: Config) -> None:
    doc = build_flow_document(config, "idea-shipped")
    assert doc is not None
    payload = flow_document_json(doc)
    import json

    serialized = json.dumps(payload)  # raises if anything isn't JSON-safe
    reloaded = json.loads(serialized)
    assert len(reloaded["stages"]) == 6
    assert reloaded["idea_slug"] == "idea-shipped"
