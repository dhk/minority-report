import csv
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from alexandria.commission import (
    ASSUMED_COMPLETION_TOKENS,
    MAX_COMPLETION_TOKENS,
    SCORE_BY_STANCE_STRENGTH,
    WEB_SEARCH_COST_USD,
    WEB_SEARCH_PROMPT_TOKENS,
    CommissionError,
    CommissionService,
    OpenRouterGateway,
    _cost_residual,
    _extracted_claims,
    _landscape,
    _normalised_for_quote_check,
    _parse_analysis,
    _quote_is_present,
    classify_scores,
    score_from_stance,
)
from alexandria.commission_models import Brief, CallRecord, CostActual, CostEstimate
from alexandria.infrastructure.config import Config
from alexandria.input_resolution import extract_input


class FakeGateway:
    def __init__(self) -> None:
        self.web_search_by_model: dict[str, bool] = {}
        self.extraction_prompts: list[str] = []
        self.graded_prompts: list[str] = []
        #: Generation ids are per call, not per model — the grader is called
        #: once per research output now, and each response is its own record.
        self.calls_made = 0

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
        self.calls_made += 1
        if model == "grader/model":
            # Two passes now: extraction fixes claim identity, then one blind
            # call per research model. They are told apart the way the real
            # grader would be — by which prompt it was handed.
            if "CLAIM LIST" in prompt:
                self.graded_prompts.append(prompt)
                supports = "alpha" in prompt
                body = json.dumps(
                    {
                        "scores": [
                            {
                                "claim_id": "c-001",
                                "stance": "supports" if supports else "silent",
                                **({"strength": "strong"} if supports else {}),
                                "quote": "remain narrow" if supports else "",
                            }
                        ]
                    }
                )
            else:
                self.extraction_prompts.append(prompt)
                body = json.dumps(
                    {
                        "claims": [
                            {
                                "claim_id": "c1",
                                "claim_text": "The provider port should remain narrow.",
                            }
                        ],
                        "report_markdown": (
                            "# Report\n\n## What this run does not establish\n\nTruth."
                        ),
                    }
                )
        else:
            # The grading prompt never names the model — that is the point of
            # §3.2 — so a fake grader can only tell outputs apart by what they
            # say, exactly as a real one would.
            body = f"Evidence from {model} says the provider port should remain narrow."
        return CallRecord(
            model_id=model,
            resolved_model_id=model + ":resolved",
            status="success",
            body=body,
            raw_response=json.dumps({"model": model, "body": body}),
            generation_id=f"gen-{model.replace('/', '-')}-{self.calls_made}",
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


class FabricatingGateway(FakeGateway):
    """A grader that returns a span the research output never contained.

    The failure this exists to catch: the pipeline recorded the quote, the
    heatmap rendered it as evidence, and publish wrote it into the public
    corpus, with nothing having compared it to the response (#47).
    """

    FABRICATION = "the port must be widened immediately"

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        call = await super().complete(model, prompt, web_search=web_search)
        if model == "grader/model" and "CLAIM LIST" in prompt and call.body:
            payload = json.loads(call.body)
            for row in payload.get("scores", []):
                if row.get("quote"):
                    row["quote"] = self.FABRICATION
            return call.model_copy(update={"body": json.dumps(payload)})
        return call


class ReflowingGateway(FakeGateway):
    """A grader quoting faithfully, but rewrapped and with typographic quotes.

    A model that rewraps a line is still quoting. If this does not verify, the
    check is punishing formatting rather than detecting invention.
    """

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        call = await super().complete(model, prompt, web_search=web_search)
        if model == "grader/model" and "CLAIM LIST" in prompt and call.body:
            payload = json.loads(call.body)
            for row in payload.get("scores", []):
                if row.get("quote"):
                    row["quote"] = "provider port\n   should  remain\tnarrow"
            return call.model_copy(update={"body": json.dumps(payload)})
        return call


class TruncatingGateway(FakeGateway):
    """One research model runs into the completion cap."""

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        call = await super().complete(model, prompt, web_search=web_search)
        if model == "beta/model":
            return call.model_copy(update={"truncated": True})
        return call


class UnescapedQuoteGateway(FakeGateway):
    """A grader that obeys "quote the span verbatim" on a span containing quotes.

    The shape is taken from run r-2026-0812-03, which was billed for grading
    and then discarded because six characters like these would not parse.
    """

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        call = await super().complete(model, prompt, web_search=web_search)
        if model != "grader/model":
            return call
        body = (
            '{"claims":[{"text":"The provider port should remain narrow.",'
            '"scores":[{"model_index":1,"stance":"supports","strength":"strong",'
            '"quote":"acknowledges \'if the honest answer is "it depends"\' here"},'
            '{"model_index":2,"stance":"silent","quote":""}]}],'
            '"report_markdown":"# Report\\n\\n## What this run does not establish\\n\\nTruth."}'
        )
        return call.model_copy(update={"body": body})


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


def test_a_valid_grading_response_is_never_rewritten() -> None:
    """Repair is a fallback, not a pass. A response that parses must come back
    byte-for-byte as the grader meant it, with no repairs claimed."""
    body = '{"claims":[],"report_markdown":"# R\\n\\n## What this run does not establish\\n\\nX."}'

    parsed, repairs = _parse_analysis(body)

    assert repairs == 0
    assert parsed["report_markdown"].startswith("# R")


def test_a_quote_containing_quotes_is_repaired_not_discarded() -> None:
    """The failure that cost run r-2026-0812-03 its whole grading pass: the
    prompt demands verbatim spans, and verbatim spans of writing about writing
    contain quotation marks."""
    body = '{"claims":[],"report_markdown":"he said "it depends" and stopped"}'

    parsed, repairs = _parse_analysis(body)

    assert repairs == 2
    assert parsed["report_markdown"] == 'he said "it depends" and stopped'


def test_a_response_with_nothing_to_repair_still_fails_honestly() -> None:
    """Truncation, a missing brace, a prose apology — none of those are this
    bug, and inventing a repair for them would hide a different failure."""
    with pytest.raises(ValueError):
        _parse_analysis('{"claims":[{"text":"cut off mid-')


@pytest.mark.anyio
async def test_a_repaired_grading_response_says_so_in_the_limitations(tmp_path: Path) -> None:
    """Silent repair would be its own kind of smoothing: an operator has to be
    able to tell a clean grading pass from a salvaged one."""
    service = CommissionService(_config(tmp_path), UnescapedQuoteGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)

    # The claim landscape survived, which is the whole point.
    claims = json.loads((service.store.run_dir(run.run_id) / "claims.json").read_text())
    assert len(claims) == 1
    assert any("unescaped quote(s)" in note for note in run.limitations)


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
    # Two research calls, then extraction plus one grading call per model.
    assert run.cost_actual == 0.05
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
    # The pair travels with the score, so the table can be rescored in place.
    assert (rows[0]["stance"], rows[0]["strength"]) == ("supports", "strong")
    assert (rows[1]["stance"], rows[1]["strength"]) == ("silent", "")


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
    # Two research calls, then grading: one extraction pass plus one blind call
    # per research model. Five calls at 0.01 in the fake gateway. The grading
    # figure is the whole pipeline, not the first leg of it.
    assert cost["actual"]["total_usd"] == pytest.approx(0.05)
    assert cost["actual"]["research_usd"] == pytest.approx(0.02)
    assert cost["actual"]["grading_usd"] == pytest.approx(0.03)
    assert cost["actual"]["billed_call_count"] == 5
    assert cost["actual"]["failed_call_count"] == 0
    # Both halves must be present, or the pair cannot be fitted later.
    assert cost["estimate"]["assumed_completion_tokens"] == ASSUMED_COMPLETION_TOKENS
    assert cost["dispatched_models"] == ["alpha/model", "beta/model"]


@pytest.mark.anyio
async def test_cost_json_records_a_per_model_breakdown_and_a_ratio(tmp_path: Path) -> None:
    # The aggregate can say a run cost 5x its estimate; only the per-model rows
    # can say which model did it, and only the ratio makes that visible without
    # reading two files and dividing by hand (#60).
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

    per_model = cost["actual"]["per_model"]
    assert len(per_model) == cost["actual"]["billed_call_count"]
    assert [row["role"] for row in per_model].count("research") == 2
    assert {row["model_id"] for row in per_model} >= {"alpha/model", "beta/model"}

    # The ratio lands on the run record too, not only in cost.json, so a
    # drifting estimate shows up in the next run rather than six runs later.
    assert run.cost_ratio == cost["residual"]["total_ratio"]
    assert draft.estimate_usd is not None
    assert run.cost_ratio == pytest.approx(0.05 / draft.estimate_usd, rel=1e-3)


def test_the_residual_measures_both_constants_against_a_real_run() -> None:
    """r-2026-0818-01, verbatim from its cost.json.

    Pinned because this run is the evidence that the two constants fail in
    opposite directions: the assumed completion length is close to right, and
    the search-prompt assumption -- 80% of a searching run's estimate -- is
    nearly 4x the measured volume. A change that "fixes the estimate" by moving
    ASSUMED_COMPLETION_TOKENS should have to break this test first.
    """
    estimate = CostEstimate(
        research_usd=1.829081,
        grading_usd=0.203394,
        web_search_usd=0.015,
        total_usd=2.047475,
        input_tokens=3798,
        grading_input_tokens=27798,
        assumed_completion_tokens=8000,
        research_model_count=3,
        worst_case_usd=2.607475,
    )
    actual = CostActual(
        research_usd=0.576406,
        grading_usd=0.172641,
        total_usd=0.749047,
        research_prompt_tokens=128586,
        research_completion_tokens=21325,
        grading_prompt_tokens=18337,
        grading_completion_tokens=7842,
        billed_call_count=4,
        failed_call_count=0,
        unpriced_call_count=0,
    )

    residual = _cost_residual(estimate, actual, web_search=True)

    # The operator was quoted 2.7x what the run cost.
    assert residual.total_ratio == pytest.approx(0.3658, abs=1e-4)
    # Completion length: 7,108 measured against 8,000 assumed. Close.
    assert residual.measured_completion_tokens_per_model == 7108
    assert residual.completion_ratio == pytest.approx(0.8885, abs=1e-4)
    # Search prompt volume: 39,064 measured against 150,000 assumed. Not close,
    # and it is the term that dominates the estimate.
    assert residual.measured_search_prompt_tokens_per_model == 39064
    assert residual.search_prompt_ratio == pytest.approx(0.2604, abs=1e-4)


def test_a_run_that_did_not_search_records_no_search_residual() -> None:
    # Subtracting the input we sent from the prompt we were billed only means
    # something when search added to it.
    estimate = CostEstimate(
        research_usd=0.1,
        grading_usd=0.05,
        web_search_usd=0.0,
        total_usd=0.15,
        input_tokens=3188,
        grading_input_tokens=19188,
        assumed_completion_tokens=8000,
        research_model_count=2,
    )
    actual = CostActual(
        research_usd=0.2,
        grading_usd=0.1,
        total_usd=0.3,
        research_prompt_tokens=8022,
        research_completion_tokens=15984,
        grading_prompt_tokens=9000,
        grading_completion_tokens=4000,
        billed_call_count=3,
        failed_call_count=0,
        unpriced_call_count=0,
    )

    residual = _cost_residual(estimate, actual, web_search=False)

    assert residual.web_search is False
    assert residual.assumed_search_prompt_tokens is None
    assert residual.measured_search_prompt_tokens_per_model is None
    assert residual.search_prompt_ratio is None
    # The completion half is still measured; it does not depend on search.
    assert residual.measured_completion_tokens_per_model == 7992
    assert residual.total_ratio == pytest.approx(2.0)


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
    # One research call survived, and it is graded by extraction plus one call.
    assert cost["actual"]["billed_call_count"] == 3


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


# --- docs/confidence-calibration.md §4: the pair, and the score derived from it


def _graded(rows: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
    """Assemble one claim from per-model grading responses, through the real code.

    ``rows`` carries the old fused shape's model_index so the existing cases
    read unchanged; here it selects which model's blind response the row lands
    in, which is what the topology change actually did to them.
    """
    claims = [{"claim_id": "c-001", "claim_text": "A declarative proposition."}]
    models = ["a/one", "b/two"]
    calls = [CallRecord(model_id=m, status="success", body="x", latency_ms=1) for m in models]
    graded: dict[str, tuple[CallRecord, dict[str, Any]]] = {}
    for index, model in enumerate(models, start=1):
        mine = [
            {k: v for k, v in row.items() if k != "model_index"} | {"claim_id": "c-001"}
            for row in rows
            if row.get("model_index") == index
        ]
        call = CallRecord(
            model_id=model, status="success", body="x", latency_ms=1, generation_id=f"gen-{index}"
        )
        graded[model] = (call, {"scores": mine})
    return _landscape(claims, graded, calls)


@pytest.mark.parametrize(
    ("stance", "strength", "expected"),
    [
        ("supports", "strong", 3),
        ("supports", "moderate", 2),
        ("supports", "weak", 1),
        ("silent", None, 0),
        ("disputes", "weak", -1),
        ("disputes", "moderate", -2),
        ("disputes", "strong", -3),
    ],
)
def test_score_is_derived_from_the_pair(stance: str, strength: str | None, expected: int) -> None:
    assert score_from_stance(stance, strength) == expected


def test_the_lookup_covers_every_non_silent_combination() -> None:
    combinations = {
        (stance, strength)
        for stance in ("supports", "disputes")
        for strength in ("strong", "moderate", "weak")
    }
    assert set(SCORE_BY_STANCE_STRENGTH) == combinations


def test_a_silent_stance_takes_no_strength() -> None:
    with pytest.raises(ValueError, match="silent stance takes no strength"):
        score_from_stance("silent", "weak")


def test_a_bearing_stance_requires_a_strength() -> None:
    with pytest.raises(ValueError, match="requires a strength"):
        score_from_stance("supports", None)


def test_an_unknown_strength_is_refused_rather_than_scored() -> None:
    with pytest.raises(ValueError, match="no score for stance"):
        score_from_stance("disputes", "mild")


def test_an_unknown_stance_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown stance"):
        _graded([{"model_index": 1, "stance": "agrees", "strength": "strong", "quote": "q"}])


def test_a_bearing_stance_without_a_quote_is_refused() -> None:
    with pytest.raises(ValueError, match="has no quote"):
        _graded([{"model_index": 1, "stance": "supports", "strength": "strong", "quote": ""}])


def test_a_silent_stance_keeps_no_quote() -> None:
    _, scores = _graded([{"model_index": 1, "stance": "silent", "quote": "leftover"}])
    assert scores[0].score == 0
    assert scores[0].stance == "silent"
    assert scores[0].strength is None
    assert scores[0].quote is None


def test_a_model_the_grader_omitted_is_silent_not_missing() -> None:
    _, scores = _graded(
        [{"model_index": 1, "stance": "disputes", "strength": "moderate", "quote": "q"}]
    )
    assert [(s.model_id, s.score, s.stance) for s in scores] == [
        ("a/one", -2, "disputes"),
        ("b/two", 0, "silent"),
    ]


def test_stored_labels_let_a_changed_mapping_be_reapplied_without_regrading() -> None:
    """The point of storing the pair: rescoring needs no model call.

    This is what no run in the corpus could do before -- scores were integers a
    model chose, with nothing recorded to recompute them from.
    """
    _, scores = _graded(
        [{"model_index": 1, "stance": "supports", "strength": "weak", "quote": "q"}]
    )
    stored = [(s.stance, s.strength) for s in scores if s.stance != "silent"]
    revised = {("supports", "weak"): 2}
    assert [revised[pair] for pair in stored] == [2]


# --- docs/confidence-calibration.md §3.1 and §3.2: the topology, not the score


@pytest.mark.anyio
async def test_grading_is_one_blind_call_per_research_model(tmp_path: Path) -> None:
    """§3.2. The property is what the grader could not see, so assert that."""
    gateway = FakeGateway()
    service = CommissionService(_config(tmp_path), gateway)
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    await service.dispatch(draft.draft_id)

    assert len(gateway.extraction_prompts) == 1
    assert len(gateway.graded_prompts) == 2
    for prompt in gateway.graded_prompts:
        # Exactly one research output is present in each grading prompt. A
        # grader that has read the other one is the defect this replaces.
        seen = [model for model in ("alpha/model", "beta/model") if f"from {model}" in prompt]
        assert len(seen) == 1, seen


@pytest.mark.anyio
async def test_claim_identity_is_fixed_before_any_output_is_scored(tmp_path: Path) -> None:
    """§3.1. Every grading call scores against the same list, supplied to it."""
    gateway = FakeGateway()
    service = CommissionService(_config(tmp_path), gateway)
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)

    for prompt in gateway.graded_prompts:
        assert "c-001: The provider port should remain narrow." in prompt
    # And the extraction pass is not asked to score anything.
    assert "stance" not in gateway.extraction_prompts[0]

    claims = json.loads((service.store.run_dir(run.run_id) / "claims.json").read_text())
    assert [c["claim_id"] for c in claims] == ["c-001"]


@pytest.mark.anyio
async def test_every_grading_leg_is_preserved_separately(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service = CommissionService(_config(tmp_path), gateway)
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)
    raw = service.store.run_dir(run.run_id) / "raw"

    assert (raw / "extraction.json").is_file()
    assert (raw / "grading-alpha-model.json").is_file()
    assert (raw / "grading-beta-model.json").is_file()
    # The scores name the grading call they came from, per model, so a
    # landscape can be traced back to the exact response that produced it.
    with (service.store.run_dir(run.run_id) / "scores.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len({row["grading_call_id"] for row in rows}) == 2


def test_extraction_renumbers_claims_rather_than_trusting_the_grader() -> None:
    claims = _extracted_claims(
        {"claims": [{"claim_id": "x", "claim_text": "One."}, {"claim_id": "x", "text": "Two."}]}
    )
    assert [c["claim_id"] for c in claims] == ["c-001", "c-002"]


def test_extraction_refuses_a_claim_with_no_text() -> None:
    with pytest.raises(ValueError, match="no declarative text"):
        _extracted_claims({"claims": [{"claim_id": "c1", "claim_text": "  "}]})


def test_extraction_refuses_an_empty_landscape() -> None:
    with pytest.raises(ValueError, match="produced no claims"):
        _extracted_claims({"claims": []})


@pytest.mark.anyio
async def test_the_run_states_its_own_apparatus(tmp_path: Path) -> None:
    """docs/instrument.md. Written by the code that graded, not by a promoter."""
    service = CommissionService(_config(tmp_path), FakeGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)

    assert run.instrument is not None
    assert run.instrument.grader_topology == "per-model-blind"
    assert run.instrument.score_derivation == "derived-lookup"
    assert run.instrument.extraction_pass == "separate"

    stored = json.loads((service.store.run_dir(run.run_id) / "run.json").read_text())
    assert stored["instrument"]["grader_topology"] == "per-model-blind"
    # Conformance is derived at read time, never stored (docs/instrument.md §3).
    assert "conforming" not in stored["instrument"]


def test_a_reflowed_quote_still_counts_as_quoting() -> None:
    body = "Evidence from alpha says the provider port\nshould remain narrow."
    # Rewrapped, and with the typographic variants a provider substitutes.
    assert _quote_is_present("provider port   should remain narrow", body)
    assert _quote_is_present("provider\tport should\nremain narrow", body)


def test_an_invented_span_is_not_quoting() -> None:
    body = "Evidence from alpha says the provider port should remain narrow."
    assert not _quote_is_present("the port must be widened immediately", body)
    # A failed research call has no body to have quoted.
    assert not _quote_is_present("anything at all", None)


def test_normalisation_folds_typography_without_changing_words() -> None:
    folded = _normalised_for_quote_check("“it depends” — the honest answer…")
    assert folded == '"it depends" - the honest answer...'


@pytest.mark.anyio
async def test_a_fabricated_quote_is_recorded_not_dropped(tmp_path: Path) -> None:
    # Rule 5: a failed observation is an observation. Dropping the quote would
    # leave a score that looks unevidenced, when what actually happened is that
    # its evidence did not check out.
    service = CommissionService(_config(tmp_path), FabricatingGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)

    rows = list(
        csv.DictReader((service.store.run_dir(run.run_id) / "scores.csv").read_text().splitlines())
    )
    quoted = [row for row in rows if row["quote"]]
    assert quoted, "the fixture must produce at least one quoted score"
    for row in quoted:
        assert row["quote"] == FabricatingGateway.FABRICATION
        assert row["quote_verified"] == "False"
        # The score itself survives; only its evidence is impeached.
        assert row["score"]

    assert any("not found in the response" in line for line in run.limitations)


@pytest.mark.anyio
async def test_a_faithful_quote_verifies_through_reflow(tmp_path: Path) -> None:
    service = CommissionService(_config(tmp_path), ReflowingGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)

    rows = list(
        csv.DictReader((service.store.run_dir(run.run_id) / "scores.csv").read_text().splitlines())
    )
    quoted = [row for row in rows if row["quote"]]
    assert quoted
    assert all(row["quote_verified"] == "True" for row in quoted)
    assert not any("not found in the response" in line for line in run.limitations)


@pytest.mark.anyio
async def test_a_score_with_no_quote_is_unchecked_rather_than_failed(tmp_path: Path) -> None:
    # A silent stance carries no quote, and a failed research call has no score
    # at all. Neither is a verification failure, and marking them False would
    # put a fabrication count on runs that fabricated nothing.
    service = CommissionService(_config(tmp_path), PartialGateway())
    draft = await service.create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        1.0,
    )
    run = await service.dispatch(draft.draft_id)

    rows = list(
        csv.DictReader((service.store.run_dir(run.run_id) / "scores.csv").read_text().splitlines())
    )
    unquoted = [row for row in rows if not row["quote"]]
    assert unquoted, "the fixture must produce a silent or failed row"
    assert all(row["quote_verified"] == "" for row in unquoted)
    assert not any("not found in the response" in line for line in run.limitations)
