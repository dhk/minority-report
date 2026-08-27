"""Promoting a finished run into the corpus (#40)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alexandria.commission import RunStore
from alexandria.commission_models import RunRecord
from alexandria.infrastructure.config import Config
from alexandria.publish import PublishError, _quotes_kept, publish_run


def _config(tmp_path: Path) -> Config:
    (tmp_path / "repo" / "research").mkdir(parents=True)
    return Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=tmp_path / "repo",
        repo_root_source="test",
    )


def _finished_run(config: Config, status: str = "completed") -> RunRecord:
    store = RunStore(config.data_dir)
    run = RunRecord(
        run_id="r-2026-0810-01",
        brief_revision="A",
        brief_sha256="abc123",
        status=status,  # type: ignore[arg-type]
        created_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        completed_at=datetime(2026, 8, 10, 12, 4, tzinfo=UTC),
        cost_actual=0.37,
        grading_model="anthropic/claude-sonnet-4.6",
        inputs=[],
        dispatched_models=["openai/gpt-5.4", "x-ai/grok-4.5"],
    )
    store.write_run(run)
    run_dir = store.run_dir(run.run_id)
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    (run_dir / "brief.md").write_text("# The question\n", encoding="utf-8")
    (run_dir / "report.md").write_text("# Findings\n", encoding="utf-8")
    (run_dir / "claims.json").write_text('[{"claim_id": "c1"}]\n', encoding="utf-8")
    (run_dir / "scores.csv").write_text(
        "claim_id,model_id,score,quote\nc1,openai/gpt-5.4,3,it said this verbatim\n",
        encoding="utf-8",
    )
    (run_dir / "raw" / "openai-gpt-5.4.json").write_text('{"body": "secret"}', encoding="utf-8")
    return run


def test_a_finished_run_lands_on_the_lifecycle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _finished_run(config)

    result = publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    root = config.repo_root / "research" / "2026-08-10-a-question"
    assert (root / "01-brief" / "brief.md").read_text(encoding="utf-8") == "# The question\n"
    assert (root / "05-analysis" / "analysis.md").read_text(encoding="utf-8") == "# Findings\n"
    assert (root / "05-analysis" / "claims.json").is_file()
    assert (root / "03-runs" / "r-2026-0810-01.json").is_file()
    assert result.written


def test_raw_provider_responses_are_never_copied(tmp_path: Path) -> None:
    """The corpus is public; the bodies stay on the host."""
    config = _config(tmp_path)
    _finished_run(config)

    publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    published = list((config.repo_root / "research").rglob("*"))
    assert not any(part.name == "raw" for part in published)
    assert not any("secret" in p.read_text(encoding="utf-8") for p in published if p.is_file())


def test_the_manifest_records_that_raw_responses_exist(tmp_path: Path) -> None:
    """An omission nobody can see reads as "there was nothing there"."""
    config = _config(tmp_path)
    _finished_run(config)

    publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    manifest = json.loads(
        (
            config.repo_root
            / "research"
            / "2026-08-10-a-question"
            / "03-runs"
            / "r-2026-0810-01.json"
        ).read_text(encoding="utf-8")
    )
    raw = manifest["raw_responses"]
    assert raw["published"] is False
    assert raw["files"][0]["file"] == "openai-gpt-5.4.json"
    assert len(raw["files"][0]["sha256"]) == 64
    assert "body" not in json.dumps(raw)


def test_extracted_quotes_do_publish(tmp_path: Path) -> None:
    """The narrow reading: evidence under a score is the point of the heatmap."""
    config = _config(tmp_path)
    _finished_run(config)

    publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    scores = (
        config.repo_root / "research" / "2026-08-10-a-question" / "05-analysis" / "scores.csv"
    ).read_text(encoding="utf-8")
    assert "it said this verbatim" in scores


def test_every_dispatched_model_appears_in_the_record(tmp_path: Path) -> None:
    """Rule 5: a model that is not published is a model that became invisible."""
    config = _config(tmp_path)
    _finished_run(config)

    publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    manifest = json.loads(
        (
            config.repo_root
            / "research"
            / "2026-08-10-a-question"
            / "03-runs"
            / "r-2026-0810-01.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["dispatched_models"] == ["openai/gpt-5.4", "x-ai/grok-4.5"]


def test_topic_yaml_leaves_the_editorial_fields_visibly_unwritten(tmp_path: Path) -> None:
    """A tool that invents framing puts fiction into the authoritative record."""
    config = _config(tmp_path)
    _finished_run(config)

    result = publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    topic = (config.repo_root / "research" / "2026-08-10-a-question" / "topic.yaml").read_text(
        encoding="utf-8"
    )
    for editorial in ("origin", "claim_under_test", "why_now", "scope"):
        assert editorial in topic
    assert topic.count("TODO") >= 4
    assert any("TODO placeholders" in item for item in result.needs_operator)


def test_nothing_is_committed(tmp_path: Path) -> None:
    """Rule 7: saving and committing is the operator's deliberate act."""
    config = _config(tmp_path)
    _finished_run(config)

    result = publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    assert "Nothing was committed" in result.summary()


def test_an_unfinished_run_is_refused_with_a_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _finished_run(config, status="running")

    with pytest.raises(PublishError, match="Only a finished run"):
        publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")


def test_republishing_refuses_rather_than_overwriting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _finished_run(config)
    publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    with pytest.raises(PublishError, match="Refusing to overwrite"):
        publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    publish_run(config, "r-2026-0810-01", "2026-08-10-a-question", overwrite=True)


def test_a_partial_run_publishes_but_says_so(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _finished_run(config, status="partial")

    result = publish_run(config, "r-2026-0810-01", "2026-08-10-a-question")

    assert any("partial" in item for item in result.needs_operator)


def test_the_public_corpus_carries_whether_a_quote_checked_out() -> None:
    """A published quote must travel with its verdict.

    Publishing evidence that was never checked, or that failed its check, with
    no way to tell either from evidence that held up, is the failure #47 named:
    the surface still looks like provenance, only the check is missing.
    """
    published = _quotes_kept(
        "claim_id,model_id,stance,strength,score,quote,quote_verified,grading_call_id\r\n"
        "c1,alpha/model,supports,strong,3,remain narrow,True,gen-1\r\n"
        "c1,beta/model,supports,weak,1,invented span,False,gen-2\r\n"
    )

    assert "quote_verified" in published.splitlines()[0]
    assert "invented span,False" in published
    assert "remain narrow,True" in published
