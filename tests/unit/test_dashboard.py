"""Composing where everything is, deterministically (#34)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alexandria.commission import RunStore
from alexandria.commission_models import RunRecord
from alexandria.dashboard import survey
from alexandria.infrastructure.config import Config

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def _config(tmp_path: Path) -> Config:
    (tmp_path / "repo" / "research").mkdir(parents=True)
    return Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=tmp_path / "repo",
        repo_root_source="test",
    )


def _run(config: Config, run_id: str, status: str, *, age_hours: float = 0.1) -> RunRecord:
    run = RunRecord(
        run_id=run_id,
        brief_revision="A",
        brief_sha256=f"sha-{run_id}",
        status=status,  # type: ignore[arg-type]
        created_at=NOW - timedelta(hours=age_hours),
        grading_model="grader/model",
        inputs=[],
        dispatched_models=["alpha/model"],
    )
    RunStore(config.data_dir).write_run(run)
    return run


def _publish(config: Config, slug: str, run_id: str) -> None:
    target = config.repo_root / "research" / slug / "03-runs"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{run_id}.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")


def test_a_finished_run_not_in_the_corpus_is_awaiting_promotion(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _run(config, "r-1", "completed")

    state = survey(config, now=NOW)

    assert [run.run_id for run in state.awaiting_promotion] == ["r-1"]
    assert state.published == []


def test_publication_is_read_from_the_corpus_not_a_second_ledger(tmp_path: Path) -> None:
    """publish writes 03-runs/<run_id>.json, so the corpus is the record."""
    config = _config(tmp_path)
    _run(config, "r-1", "completed")
    _publish(config, "2026-08-10-a-question", "r-1")

    state = survey(config, now=NOW)

    assert [run.run_id for run in state.published] == ["r-1"]
    assert state.awaiting_promotion == []


def test_a_long_running_record_is_flagged_rather_than_counted_as_progress(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _run(config, "r-stale", "running", age_hours=64)
    _run(config, "r-fresh", "running", age_hours=0.05)

    state = survey(config, now=NOW)

    assert [run.run_id for run in state.running] == ["r-fresh"]
    assert [run.run_id for run, _ in state.needs_attention] == ["r-stale"]
    assert "restarted mid-run" in state.needs_attention[0][1]


def test_failed_and_partial_runs_need_attention(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _run(config, "r-failed", "failed")
    _run(config, "r-partial", "partial")

    state = survey(config, now=NOW)

    reasons = {run.run_id: reason for run, reason in state.needs_attention}
    assert "no models produced usable output" in reasons["r-failed"]
    assert "not a full panel" in reasons["r-partial"]


def test_actions_are_derived_from_state_and_carry_their_reason(tmp_path: Path) -> None:
    """A task list nobody can check is worse than none — every action says why."""
    config = _config(tmp_path)
    _run(config, "r-1", "completed")
    _run(config, "r-stale", "running", age_hours=64)

    actions = survey(config, now=NOW).actions

    assert actions[0].label == "Look at run r-stale", "most urgent first"
    assert all(action.why for action in actions)
    assert all(action.href.startswith("/") for action in actions)
    assert any("Promote run r-1" in action.label for action in actions)


def test_the_page_declares_what_it_cannot_see(tmp_path: Path) -> None:
    """A total that quietly excludes things reads as a total."""
    state = survey(_config(tmp_path), now=NOW)

    blind = " ".join(state.blind_spots)
    assert "another process" in blind
    assert "GitHub issues" in blind
    assert "website" in blind


def test_an_empty_host_produces_an_empty_survey_not_an_error(tmp_path: Path) -> None:
    state = survey(_config(tmp_path), now=NOW)

    assert state.actions == []
    assert state.running == []
    assert state.investigations == []
