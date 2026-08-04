import csv
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from alexandria.commission import (
    WEB_SEARCH_COST_USD,
    CommissionError,
    CommissionService,
    OpenRouterGateway,
    classify_scores,
)
from alexandria.commission_models import Brief, CallRecord
from alexandria.infrastructure.config import Config
from alexandria.input_resolution import extract_input


class FakeGateway:
    def __init__(self) -> None:
        self.web_search_by_model: dict[str, bool] = {}

    async def estimate(
        self, models: list[str], input_tokens: int, *, web_search: bool = False
    ) -> float:
        assert input_tokens > 0
        return 0.25

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        self.web_search_by_model[model] = web_search
        if model == "grader/model":
            body = json.dumps(
                {
                    "claims": [
                        {
                            "text": "The provider port should remain narrow.",
                            "scores": [
                                {"model_index": 1, "score": 3, "quote": "remain narrow"},
                                {"model_index": 2, "score": 0, "quote": ""},
                            ],
                        }
                    ],
                    "report_markdown": "# Report\n\n## What this run does not establish\n\nTruth.",
                }
            )
        else:
            body = "Evidence says the provider port should remain narrow."
        return CallRecord(
            model_id=model,
            resolved_model_id=model + ":resolved",
            status="success",
            body=body,
            raw_response=json.dumps({"model": model, "body": body}),
            generation_id="gen-" + model.replace("/", "-"),
            cost=0.01,
            latency_ms=10,
        )


class PartialGateway(FakeGateway):
    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        if model == "beta/model":
            return CallRecord(
                model_id=model,
                status="failed",
                error="OpenRouter HTTP 503",
                status_code=503,
                latency_ms=10,
            )
        return await super().complete(model, prompt, web_search=web_search)


def _config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=tmp_path / "repo",
        repo_root_source="test",
    )


def _pricing_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "alpha/model",
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                }
            ]
        },
    )


def _gateway_capturing(sent: list[dict[str, Any]]) -> OpenRouterGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _pricing_response()
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"cost": 0.01},
            },
        )

    client = httpx.AsyncClient(
        base_url="https://openrouter.test", transport=httpx.MockTransport(handler)
    )
    return OpenRouterGateway("test-key", client)


@pytest.mark.anyio
async def test_web_search_sends_the_openrouter_web_plugin() -> None:
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        call = await gateway.complete("alpha/model", "prompt", web_search=True)

    assert call.status == "success"
    # The model id stays canonical; ":online" would break the /models lookup.
    assert sent[0]["model"] == "alpha/model"
    assert sent[0]["plugins"] == [{"id": "web", "max_results": 5}]


@pytest.mark.anyio
async def test_without_web_search_no_plugin_field_is_sent() -> None:
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        await gateway.complete("alpha/model", "prompt")

    assert "plugins" not in sent[0]


@pytest.mark.anyio
async def test_estimate_charges_for_each_searching_model() -> None:
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        plain = await gateway.estimate(["alpha/model"], 1_000)
        searching = await gateway.estimate(["alpha/model"], 1_000)
        with_search = await gateway.estimate(["alpha/model"], 1_000, web_search=True)

    assert plain == searching
    assert with_search == pytest.approx(plain + WEB_SEARCH_COST_USD)


def test_claim_group_precedence() -> None:
    assert classify_scores([3, -1, 0], 3) == "disagreement"
    assert classify_scores([2, 0, 0], 3) == "novel"
    assert classify_scores([0, 0], 2) == "silent"
    assert classify_scores([2, 1], 3) == "consensus"
    assert classify_scores([2, 1], 5) == "thin"


@pytest.mark.anyio
async def test_commission_persists_accepted_run_shapes(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)
    run_dir = service.store.run_dir(run.run_id)

    assert run.status == "completed"
    assert run.cost_actual == 0.03
    assert service.store.load_run(run.run_id).run_id == run.run_id
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "claims.json").is_file()
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / "raw/alpha-model.json").is_file()
    run_input = json.loads((run_dir / "run.json").read_text())["inputs"][0]
    assert "text" not in run_input
    assert "original_base64" not in run_input
    assert next((run_dir / "inputs/original").iterdir()).read_bytes() == b"Keep the port narrow."

    with (run_dir / "scores.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["score"] == "3"
    assert rows[1]["score"] == "0"


@pytest.mark.anyio
async def test_research_searches_the_web_by_default_but_grading_does_not(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service = CommissionService(_config(tmp_path), gateway)
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    assert draft.web_search is True
    run = await service.dispatch(draft.draft_id)

    assert gateway.web_search_by_model == {
        "alpha/model": True,
        "beta/model": True,
        # Grading reads the research bodies; searching there would let the
        # grader introduce evidence no research model ever saw.
        "grader/model": False,
    }
    assert run.web_search is True
    assert any("searched the live web" in note for note in run.limitations)


@pytest.mark.anyio
async def test_web_search_off_keeps_the_run_reproducible(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service = CommissionService(_config(tmp_path), gateway)
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
        web_search=False,
    )
    run = await service.dispatch(draft.draft_id)

    assert set(gateway.web_search_by_model.values()) == {False}
    assert run.web_search is False
    assert not any("searched the live web" in note for note in run.limitations)


@pytest.mark.anyio
async def test_dispatch_refuses_estimate_over_ceiling(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        0.10,
    )
    with pytest.raises(CommissionError, match="exceeds ceiling"):
        await service.dispatch(draft.draft_id)
    assert service.store.list_runs() == []


@pytest.mark.anyio
async def test_failed_call_is_missing_observation_not_silence(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), PartialGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)
    assert run.status == "partial"
    with (service.store.run_dir(run.run_id) / "scores.csv").open(
        newline="", encoding="utf-8"
    ) as stream:
        rows = list(csv.DictReader(stream))
    failed = next(row for row in rows if row["model_id"] == "beta/model")
    assert failed["score"] == ""
    assert any("missing observations (✕), not silence" in item for item in run.limitations)
