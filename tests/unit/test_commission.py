import csv
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from alexandria.commission import (
    ASSUMED_COMPLETION_TOKENS,
    MAX_COMPLETION_TOKENS,
    WEB_SEARCH_COST_USD,
    WEB_SEARCH_PROMPT_TOKENS,
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
        self,
        models: list[str],
        input_tokens: int,
        *,
        web_search: bool = False,
        completion_tokens: int = ASSUMED_COMPLETION_TOKENS,
    ) -> float:
        assert input_tokens > 0
        # The worst case asks the same question with the cap substituted, so a
        # fake that ignored completion_tokens would make the bound untestable.
        return 0.25 * (completion_tokens / ASSUMED_COMPLETION_TOKENS)

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


class TruncatingGateway(FakeGateway):
    """One research model runs into the completion cap."""

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        call = await super().complete(model, prompt, web_search=web_search)
        if model == "beta/model":
            return call.model_copy(update={"truncated": True})
        return call


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
async def test_every_call_names_its_own_completion_cap() -> None:
    """OpenRouter fills in a max_tokens when a request omits one, and reserves
    ``max_tokens x completion price`` against the key's credit before
    generating anything. Omitting the field meant reserving 65,536 tokens a
    call — a $0.29 three-model run had to hold ~$4 to start, and did not
    (#50). The cap has to be ours, and it has to be sent."""
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        await gateway.complete("alpha/model", "prompt")

    assert sent[0]["max_tokens"] == MAX_COMPLETION_TOKENS


@pytest.mark.anyio
async def test_an_answer_stopped_by_the_cap_is_recorded_as_truncated() -> None:
    """A cut-off answer is a partial observation, not a complete one. The body
    is kept; what must not happen is presenting it as a finished answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _pricing_response()
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "choices": [{"message": {"content": "half an ans"}, "finish_reason": "length"}],
                "usage": {"cost": 0.01},
            },
        )

    client = httpx.AsyncClient(
        base_url="https://openrouter.test", transport=httpx.MockTransport(handler)
    )
    async with OpenRouterGateway("test-key", client) as gateway:
        call = await gateway.complete("alpha/model", "prompt")

    assert call.status == "success"
    assert call.truncated is True
    assert call.body == "half an ans"


@pytest.mark.anyio
async def test_an_answer_that_finished_is_not_marked_truncated() -> None:
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        call = await gateway.complete("alpha/model", "prompt")

    assert call.truncated is False


@pytest.mark.anyio
async def test_a_truncated_answer_says_so_in_the_run_limitations(tmp_path: Path) -> None:
    """Silence from a model that ran out of room is the cap talking, not the
    model declining to address a claim — the run has to be able to tell an
    operator which it was."""
    service = CommissionService(_config(tmp_path), TruncatingGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)

    assert any("beta/model was cut off" in note for note in run.limitations)
    assert not any("alpha/model was cut off" in note for note in run.limitations)


@pytest.mark.anyio
async def test_without_web_search_no_plugin_field_is_sent() -> None:
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        await gateway.complete("alpha/model", "prompt")

    assert "plugins" not in sent[0]


def _gateway_returning(body: str) -> OpenRouterGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _pricing_response()
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(
        base_url="https://openrouter.test", transport=httpx.MockTransport(handler)
    )
    return OpenRouterGateway("test-key", client)


_COMPLETION_BODY = json.dumps(
    {
        "id": "gen-1",
        "model": "alpha/model",
        "choices": [{"message": {"content": "answer"}}],
        "usage": {"cost": 0.01},
    }
)


@pytest.mark.anyio
async def test_keep_alive_padding_does_not_lose_a_billed_model() -> None:
    # OpenRouter pads long-running non-streaming responses with SSE comment
    # lines. The 200 is real and already billed, so the model must survive it.
    padded = ": OPENROUTER PROCESSING\n" * 3 + _COMPLETION_BODY
    async with _gateway_returning(padded) as gateway:
        call = await gateway.complete("alpha/model", "prompt")

    assert call.status == "success"
    assert call.body == "answer"
    assert call.cost == 0.01
    assert call.generation_id == "gen-1"
    # The padded bytes are preserved verbatim as received.
    assert call.raw_response == padded


@pytest.mark.anyio
async def test_trailing_padding_and_surrounding_whitespace_are_tolerated() -> None:
    async with _gateway_returning(
        f"\n\n  {_COMPLETION_BODY}\n: OPENROUTER PROCESSING\n"
    ) as gateway:
        call = await gateway.complete("alpha/model", "prompt")

    assert call.status == "success"
    assert call.body == "answer"


@pytest.mark.anyio
async def test_a_genuinely_undecodable_body_still_fails_with_an_excerpt() -> None:
    # Tolerating padding must not turn real garbage into a silent success.
    async with _gateway_returning("<html>gateway timeout</html>") as gateway:
        call = await gateway.complete("alpha/model", "prompt")

    assert call.status == "failed"
    assert call.status_code == 200
    assert call.raw_response == "<html>gateway timeout</html>"
    # The excerpt is what makes the next occurrence diagnosable at all.
    assert "gateway timeout" in (call.error or "")


@pytest.mark.anyio
async def test_an_empty_body_is_a_failure_not_an_empty_answer() -> None:
    async with _gateway_returning(": OPENROUTER PROCESSING\n") as gateway:
        call = await gateway.complete("alpha/model", "prompt")

    assert call.status == "failed"
    assert not call.body


@pytest.mark.anyio
async def test_a_non_object_completion_body_fails_the_call_not_the_run() -> None:
    async with _gateway_returning("[1, 2, 3]") as gateway:
        call = await gateway.complete("alpha/model", "prompt")

    assert call.status == "failed"


@pytest.mark.anyio
async def test_estimate_prices_search_as_tokens_not_just_a_per_request_fee() -> None:
    """The old estimate added $0.005 a search and called it done, which is why a
    $0.2873 run cost $3.03: results come back as PROMPT tokens on the same call,
    and 165k of them per model is the actual bill (#49)."""
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        plain = await gateway.estimate(["alpha/model"], 1_000)
        with_search = await gateway.estimate(["alpha/model"], 1_000, web_search=True)

    prompt_price = 0.000001  # _pricing_response
    assert with_search == pytest.approx(
        plain + WEB_SEARCH_COST_USD + WEB_SEARCH_PROMPT_TOKENS * prompt_price
    )


@pytest.mark.anyio
async def test_the_worst_case_prices_the_cap_not_the_assumption() -> None:
    """The number the ceiling is checked against. Same prices, completion length
    swapped for the cap every call is now sent with."""
    sent: list[dict[str, Any]] = []
    async with _gateway_capturing(sent) as gateway:
        assumed = await gateway.estimate(["alpha/model"], 1_000)
        bound = await gateway.estimate(
            ["alpha/model"], 1_000, completion_tokens=MAX_COMPLETION_TOKENS
        )

    completion_price = 0.000002  # _pricing_response
    assert bound - assumed == pytest.approx(
        (MAX_COMPLETION_TOKENS - ASSUMED_COMPLETION_TOKENS) * completion_price
    )


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
async def test_research_searches_when_asked_but_grading_never_does(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service = CommissionService(_config(tmp_path), gateway)
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        4.0,
        web_search=True,
        web_search_rationale="The port's current upstream behaviour changed this month.",
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
    with pytest.raises(CommissionError, match="an estimate is not a cap"):
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


@pytest.mark.anyio
async def test_estimate_components_account_for_the_whole_total(tmp_path: Path) -> None:
    # The operator approves a number; the components must add up to that number
    # or the breakdown is decoration.
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        4.0,
        web_search=True,
        web_search_rationale="Needs sources published after training cutoff.",
    )
    detail = draft.estimate_detail
    assert detail is not None
    assert detail.total_usd == draft.estimate_usd
    summed = detail.research_usd + detail.grading_usd + detail.web_search_usd
    assert summed == pytest.approx(detail.total_usd)
    # Web search is on by default, so it must be visible as its own term.
    assert detail.web_search_usd == pytest.approx(WEB_SEARCH_COST_USD * 2)
    assert detail.research_model_count == 2


@pytest.mark.anyio
async def test_web_search_off_removes_the_search_term(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
        web_search=False,
    )
    assert draft.estimate_detail is not None
    assert draft.estimate_detail.web_search_usd == 0.0


@pytest.mark.anyio
async def test_cost_json_records_prediction_against_measurement(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)
    cost = json.loads((service.store.run_dir(run.run_id) / "cost.json").read_text(encoding="utf-8"))

    assert cost["estimate"]["total_usd"] == draft.estimate_usd
    # Two research calls plus grading, each 0.01 in the fake gateway.
    assert cost["actual"]["total_usd"] == pytest.approx(0.03)
    assert cost["actual"]["research_usd"] == pytest.approx(0.02)
    assert cost["actual"]["grading_usd"] == pytest.approx(0.01)
    assert cost["actual"]["billed_call_count"] == 3
    assert cost["actual"]["failed_call_count"] == 0
    # Both halves must be present, or the pair cannot be fitted later.
    assert cost["estimate"]["assumed_completion_tokens"] == ASSUMED_COMPLETION_TOKENS
    assert cost["dispatched_models"] == ["alpha/model", "beta/model"]


@pytest.mark.anyio
async def test_a_failed_call_is_still_counted_in_the_cost_record(tmp_path: Path) -> None:
    # A model that fails after billable output still spent money. Dropping it
    # from the record is exactly how an overrun stays invisible.
    service = CommissionService(_config(tmp_path), PartialGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)
    cost = json.loads((service.store.run_dir(run.run_id) / "cost.json").read_text(encoding="utf-8"))

    assert cost["actual"]["failed_call_count"] == 1
    assert cost["actual"]["unpriced_call_count"] == 1
    assert cost["actual"]["billed_call_count"] == 2


@pytest.mark.anyio
async def test_cost_json_is_listed_as_a_run_artifact(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)
    manifest = json.loads(
        (service.store.run_dir(run.run_id) / "manifest.json").read_text(encoding="utf-8")
    )
    assert "cost.json" in manifest["artifacts"]


@pytest.mark.anyio
async def test_the_ceiling_is_checked_against_the_bound_not_the_prediction(
    tmp_path: Path,
) -> None:
    """r-2026-0812-03 was estimated at $0.2873, cleared a $1.00 ceiling that
    called itself 'the only bound that is enforced', and cost $3.0321. A ceiling
    compared only against an estimate stops nothing (#49)."""
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        0.6,
    )
    detail = draft.estimate_detail
    assert detail is not None and detail.worst_case_usd is not None
    # The estimate fits; the bound does not. Before this, that dispatched.
    assert draft.estimate_usd is not None and draft.estimate_usd < 0.6
    assert detail.worst_case_usd > 0.6

    with pytest.raises(CommissionError, match="an estimate is not a cap"):
        await service.dispatch(draft.draft_id)

    assert service.store.list_runs() == []


@pytest.mark.anyio
async def test_web_search_without_a_reason_is_refused(tmp_path: Path) -> None:
    """The expensive option has to say what it is for. This is the only moment
    anyone is made to ask whether the brief needs live sources at all."""
    service = CommissionService(_config(tmp_path), FakeGateway())

    with pytest.raises(CommissionError, match="Web search needs a reason"):
        await service.create_draft(
            Brief(task="Assess the provider port."),
            [extract_input("brief.md", b"Keep the port narrow.")],
            ["alpha/model", "beta/model"],
            "grader/model",
            4.0,
            web_search=True,
        )


@pytest.mark.anyio
async def test_the_reason_is_carried_into_the_draft_for_the_operator_to_approve(
    tmp_path: Path,
) -> None:
    """Approving a flag is not approving a reason. The review shows the words."""
    service = CommissionService(_config(tmp_path), FakeGateway())

    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        4.0,
        web_search=True,
        web_search_rationale="  The upstream spec changed after the training cutoff.  ",
    )

    assert draft.web_search_rationale == "The upstream spec changed after the training cutoff."


@pytest.mark.anyio
async def test_search_is_off_unless_asked_for(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), FakeGateway())

    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        4.0,
    )

    assert draft.web_search is False
    assert draft.web_search_rationale == ""
