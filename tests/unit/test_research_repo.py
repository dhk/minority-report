from pathlib import Path

import pytest

from alexandria.infrastructure.config import Config
from alexandria.infrastructure.research_repo import (
    ResearchRepoError,
    find_investigation,
    list_investigations,
    search_investigations,
)


def _config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=tmp_path / "repo",
        repo_root_source="test",
    )


def _write_investigation(
    repo_root: Path,
    slug: str,
    *,
    title: str | None = None,
    assurance_level: str | None = None,
    status: str | None = None,
    stages: tuple[str, ...] = (),
) -> Path:
    investigation_dir = repo_root / "research" / slug
    investigation_dir.mkdir(parents=True)
    fields = []
    if title is not None:
        fields.append(f"title: {title}")
    if assurance_level is not None:
        fields.append(f"assurance_level: {assurance_level}")
    if status is not None:
        fields.append(f"status: {status}")
    (investigation_dir / "topic.yaml").write_text("\n".join(fields) + "\n", encoding="utf-8")
    for stage in stages:
        stage_dir = investigation_dir / stage
        stage_dir.mkdir()
        (stage_dir / "placeholder.md").write_text("content", encoding="utf-8")
    return investigation_dir


def test_list_investigations_empty_when_no_research_dir(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert list_investigations(config) == []


def test_list_investigations_reads_topic_yaml(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_investigation(
        config.repo_root,
        "2026-07-28-does-alexandria-need-to-exist",
        title="Does Alexandria need to exist?",
        assurance_level="Bronze",
        status="in-progress",
        stages=("00-topic", "01-brief"),
    )
    investigations = list_investigations(config)
    assert len(investigations) == 1
    investigation = investigations[0]
    assert investigation.slug == "2026-07-28-does-alexandria-need-to-exist"
    assert investigation.title == "Does Alexandria need to exist?"
    assert investigation.assurance_level == "bronze"
    assert investigation.status == "in-progress"
    assert investigation.stages_present == ("00-topic", "01-brief")
    assert investigation.current_stage == "01-brief"


def test_list_investigations_handles_missing_topic_yaml(tmp_path: Path) -> None:
    config = _config(tmp_path)
    investigation_dir = config.repo_root / "research" / "no-topic-yet"
    investigation_dir.mkdir(parents=True)
    investigations = list_investigations(config)
    assert investigations[0].title is None
    assert investigations[0].assurance_level is None
    assert investigations[0].current_stage is None


def test_list_investigations_raises_on_invalid_yaml(tmp_path: Path) -> None:
    config = _config(tmp_path)
    investigation_dir = config.repo_root / "research" / "broken"
    investigation_dir.mkdir(parents=True)
    (investigation_dir / "topic.yaml").write_text("title: [unclosed", encoding="utf-8")
    with pytest.raises(ResearchRepoError):
        list_investigations(config)


def test_find_investigation_by_slug(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_investigation(config.repo_root, "alpha", title="Alpha")
    _write_investigation(config.repo_root, "beta", title="Beta")
    found = find_investigation(config, "beta")
    assert found is not None
    assert found.title == "Beta"
    assert find_investigation(config, "missing") is None


def test_search_investigations_finds_matches_with_line_numbers(tmp_path: Path) -> None:
    config = _config(tmp_path)
    investigation_dir = _write_investigation(config.repo_root, "alpha", title="Alpha")
    (investigation_dir / "README.md").write_text(
        "line one\nsomething about warp drives\nline three\n", encoding="utf-8"
    )
    hits = search_investigations(config, "warp drives", limit=10)
    assert len(hits) == 1
    assert hits[0].slug == "alpha"
    assert hits[0].relative_path == "README.md"
    assert hits[0].line_number == 2
    assert "warp drives" in hits[0].snippet


def test_search_investigations_respects_limit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    investigation_dir = _write_investigation(config.repo_root, "alpha", title="Alpha")
    (investigation_dir / "README.md").write_text("needle\n" * 5, encoding="utf-8")
    hits = search_investigations(config, "needle", limit=2)
    assert len(hits) == 2


def test_search_investigations_empty_query_returns_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_investigation(config.repo_root, "alpha", title="Alpha")
    assert search_investigations(config, "   ", limit=10) == []
