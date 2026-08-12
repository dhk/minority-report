"""Commission review, OpenRouter dispatch, analysis, and immutable run persistence."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self

import httpx

from alexandria.commission_models import (
    Brief,
    CallRecord,
    ClaimGroup,
    ClaimRecord,
    CostActual,
    CostEstimate,
    Draft,
    InputArtifact,
    RunRecord,
    ScoreRecord,
)
from alexandria.infrastructure.config import Config

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Four distinct training lineages, deliberately. This product exists to surface
# disagreement, and models from one family agree with each other for reasons
# that are not evidence. grok-4.5 replaced google/gemini-3.1-pro-preview: the
# Google entry was a preview build, and swapping it for another Google model
# would have kept the panel at three families.
DEFAULT_MODELS = [
    "openai/gpt-5.4",
    "anthropic/claude-opus-4.7",
    "x-ai/grok-4.5",
]
DEFAULT_GRADING_MODEL = "anthropic/claude-sonnet-4.6"

# OpenRouter's web plugin, documented as exactly equivalent to the ``:online``
# model suffix. The suffix is not a catalogue entry, so it would break the
# ``/models`` pricing lookup; the plugin keeps model ids canonical.
WEB_PLUGIN_MAX_RESULTS = 5
# Exa (the default engine) bills $0.005 per request for up to 10 results.
WEB_SEARCH_COST_USD = 0.005
# What search costs that the per-request fee does not: its results come back as
# PROMPT tokens on the next call. Measured on one brief run both ways --
# 529,883 research prompt tokens with search on against 36,263 with it off,
# across three models. That is ~165k extra per searching model, and it is why a
# run estimated at $0.2873 cost $3.03 (#49). One observation, not a fit; the
# review says so rather than implying this number is reliable.
WEB_SEARCH_PROMPT_TOKENS = 150_000
# What a research answer actually runs to, measured across every completed call
# in r-2026-0812-03 and -04: 7,228 / 7,802 / 8,300 / 10,744 completion tokens.
# The old value here was 2_000, which under-priced every run by about 4x before
# search entered the picture and made "the estimate" quietly useless (#49).
ASSUMED_COMPLETION_TOKENS = 8_000
# The cap sent as ``max_tokens`` on every call. Deliberately far above
# ASSUMED_COMPLETION_TOKENS: that constant predicts a typical answer for
# pricing, this one bounds the longest answer worth paying for. Observed
# research answers run 8k-13k completion tokens, so this leaves headroom
# without inviting a runaway.
#
# Sending it at all is the point. OpenRouter fills in a max_tokens when a
# request omits one (undocumented as prose; visible in its own
# debug.echo_upstream_body example) and then reserves
# ``max_tokens x completion price`` against the key's credit BEFORE
# generating a token. Omitting the field meant reserving 65,536 tokens per
# call -- $1.64 for one Opus call, $3.01 for a three-model dispatch whose
# estimate was $0.29 -- and a 402 that killed two calls of run
# r-2026-0812-02 outright. See issue #50.
MAX_COMPLETION_TOKENS = 16_000
# The corpus owns this contract: dhk/alexandria's schemas/claim-score.schema.json.
# Mirrored here because dispatch must enforce it without a corpus checkout to hand;
# tests/unit/test_corpus_contract.py fails if the two ever disagree.
SCORE_MIN = -3
SCORE_MAX = 3


class CommissionError(RuntimeError):
    """A commission cannot safely advance to its next lifecycle state."""


def _decode_openrouter_json(raw: str) -> Any:
    """Parse an OpenRouter body, tolerating keep-alive padding around the JSON.

    OpenRouter holds long-running connections open by emitting SSE comment
    lines (``: OPENROUTER PROCESSING``) before the body, including on
    non-streaming requests. ``httpx``'s ``.json()`` rejects the padded body, so
    a 200 that OpenRouter had already billed us for was recorded as a failed
    call and the model dropped out of the run — see issue #30.

    Only leading comment lines are stripped, and decoding stops at the end of
    the first JSON value, so trailing padding is ignored too. Neither step can
    alter a valid JSON payload: a literal line-initial ``:`` cannot occur
    inside one, because JSON strings escape their newlines.
    """
    text = raw.lstrip()
    while text.startswith(":"):
        _, _, text = text.partition("\n")
        text = text.lstrip()
    if not text:
        raise ValueError("OpenRouter returned an empty body")
    value, _end = json.JSONDecoder().raw_decode(text)
    return value


def _body_excerpt(raw: str, limit: int = 200) -> str:
    """A short, single-line excerpt of a body, for diagnosing a decode failure."""
    excerpt = raw[:limit].replace("\n", "\\n").replace("\r", "\\r")
    return excerpt + "…" if len(raw) > limit else excerpt


class Gateway(Protocol):
    async def estimate(
        self,
        models: list[str],
        input_tokens: int,
        *,
        web_search: bool = False,
        completion_tokens: int = ASSUMED_COMPLETION_TOKENS,
    ) -> float: ...

    async def complete(
        self, model: str, prompt: str, *, web_search: bool = False
    ) -> CallRecord: ...


def _safe_model_name(model_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", model_id).strip("-")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _strip_input_text(item: InputArtifact) -> dict[str, object]:
    return item.model_dump(exclude={"text", "original_base64"})


def classify_scores(scores: list[int], responding_models: int) -> ClaimGroup:
    """Apply RFC-0005's decided group precedence to one claim."""
    bearing = [score for score in scores if score != 0]
    signs = {1 if score > 0 else -1 for score in bearing}
    if len(signs) > 1:
        return "disagreement"
    if not bearing:
        return "silent"
    if len(bearing) == 1:
        return "novel"
    if len(bearing) * 2 < responding_models:
        return "thin"
    return "consensus"


class RunStore:
    """Local scratch drafts and immutable directory-per-run records."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.drafts_dir = data_dir / "drafts"
        self.runs_dir = data_dir / "runs"

    def save_draft(self, draft: Draft) -> None:
        _write_atomic(
            self.drafts_dir / f"{draft.draft_id}.json", _json_bytes(draft.model_dump(mode="json"))
        )

    def load_draft(self, draft_id: str) -> Draft:
        path = self.drafts_dir / f"{draft_id}.json"
        if not path.is_file():
            raise CommissionError(f"Draft {draft_id!r} does not exist.")
        return Draft.model_validate_json(path.read_text(encoding="utf-8"))

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def next_run_id(self, now: datetime) -> str:
        prefix = f"r-{now:%Y-%m%d}-"
        existing = [path.name for path in self.runs_dir.glob(f"{prefix}*")]
        return f"{prefix}{len(existing) + 1:02d}"

    def list_runs(self) -> list[RunRecord]:
        if not self.runs_dir.is_dir():
            return []
        records: list[RunRecord] = []
        for path in sorted(self.runs_dir.glob("*/run.json"), reverse=True):
            try:
                records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records

    def load_run(self, run_id: str) -> RunRecord:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            raise CommissionError(f"Run {run_id!r} does not exist.")
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def write_run(self, run: RunRecord) -> None:
        data = run.model_dump(mode="json")
        data["inputs"] = [_strip_input_text(item) for item in run.inputs]
        _write_atomic(self.run_dir(run.run_id) / "run.json", _json_bytes(data))


class OpenRouterGateway:
    """Small OpenRouter adapter that preserves raw responses and aggregator cost."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-OpenRouter-Title": "Alexandria",
            },
            timeout=180,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def estimate(
        self,
        models: list[str],
        input_tokens: int,
        *,
        web_search: bool = False,
        completion_tokens: int = ASSUMED_COMPLETION_TOKENS,
    ) -> float:
        """Priced cost for one leg of a run.

        ``completion_tokens`` is what separates the estimate from the bound:
        pass the assumed length for a prediction, or MAX_COMPLETION_TOKENS for
        the most this leg can possibly cost. Both come from the same live
        prices, so they cannot drift apart.
        """
        response = await self.client.get("/models")
        response.raise_for_status()
        payload = _decode_openrouter_json(response.text)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CommissionError("OpenRouter returned an unexpected model-list response.")
        by_id = {str(row.get("id")): row for row in rows if isinstance(row, dict)}
        total = 0.0
        for model in models:
            row = by_id.get(model)
            pricing = row.get("pricing") if isinstance(row, dict) else None
            if not isinstance(pricing, dict):
                raise CommissionError(f"No live OpenRouter pricing found for {model}.")
            prompt = float(pricing.get("prompt") or 0)
            completion = float(pricing.get("completion") or 0)
            request = float(pricing.get("request") or 0)
            # Search results arrive as prompt tokens on this same call, so they
            # are priced as tokens here and not only as the per-request fee.
            prompt_tokens = input_tokens + (WEB_SEARCH_PROMPT_TOKENS if web_search else 0)
            total += prompt_tokens * prompt + completion_tokens * completion + request
            if web_search:
                total += WEB_SEARCH_COST_USD
        return round(total, 6)

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        started = time.monotonic()
        request_body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": MAX_COMPLETION_TOKENS,
        }
        if web_search:
            request_body["plugins"] = [{"id": "web", "max_results": WEB_PLUGIN_MAX_RESULTS}]
        try:
            response = await self.client.post(
                "/chat/completions",
                headers={"X-OpenRouter-Metadata": "enabled"},
                json=request_body,
            )
        except httpx.HTTPError as exc:
            return CallRecord(
                model_id=model,
                status="failed",
                latency_ms=round((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
            )
        latency_ms = round((time.monotonic() - started) * 1000)
        raw = response.text
        generation_id = response.headers.get("X-Generation-Id")
        if response.status_code >= 400:
            return CallRecord(
                model_id=model,
                status="failed",
                raw_response=raw,
                generation_id=generation_id,
                latency_ms=latency_ms,
                error=f"OpenRouter HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload = _decode_openrouter_json(raw)
            if not isinstance(payload, dict):
                raise TypeError("completion response is not a JSON object")
            choices = payload.get("choices")
            first = choices[0] if isinstance(choices, list) and choices else {}
            message = first.get("message") if isinstance(first, dict) else {}
            body = message.get("content") if isinstance(message, dict) else None
            usage = payload.get("usage") if isinstance(payload, dict) else {}
            usage = usage if isinstance(usage, dict) else {}
            resolved_generation_id = generation_id or str(payload.get("id") or "") or None
            cost = _optional_float(usage.get("cost"))
            if cost is None and resolved_generation_id:
                try:
                    generation = await self.client.get(
                        "/generation", params={"id": resolved_generation_id}
                    )
                    generation.raise_for_status()
                    generation_payload = _decode_openrouter_json(generation.text)
                    generation_data = (
                        generation_payload.get("data")
                        if isinstance(generation_payload, dict)
                        else None
                    )
                    if isinstance(generation_data, dict):
                        cost = _optional_float(generation_data.get("total_cost"))
                except (httpx.HTTPError, ValueError):
                    pass
            # A cap that cuts an answer off must say so. The body is still
            # worth keeping -- a truncated research answer is a partial
            # observation, not a failed call -- but presenting it as a
            # complete one would be exactly the silent smoothing this
            # repository exists to avoid.
            finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
            return CallRecord(
                model_id=model,
                resolved_model_id=str(payload.get("model") or model),
                status="success",
                body=str(body or ""),
                raw_response=raw,
                generation_id=resolved_generation_id,
                prompt_tokens=_optional_int(usage.get("prompt_tokens")),
                completion_tokens=_optional_int(usage.get("completion_tokens")),
                cost=cost,
                latency_ms=latency_ms,
                truncated=finish_reason == "length",
            )
        except (ValueError, TypeError, KeyError) as exc:
            return CallRecord(
                model_id=model,
                status="failed",
                raw_response=raw,
                generation_id=generation_id,
                latency_ms=latency_ms,
                error=(f'Invalid OpenRouter response: {exc} (body starts: "{_body_excerpt(raw)}")'),
                status_code=response.status_code,
            )


def _sum_optional(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _cost_actual(calls: list[CallRecord], grading_call: CallRecord | None) -> CostActual:
    """Measure what a run really cost, in the same shape as its estimate.

    Every dispatched call counts, including the ones that failed: a model that
    errored after billable output still spent money, and dropping it here is
    exactly how an overrun stays invisible.
    """
    every_call = [call for call in [*calls, grading_call] if call is not None]
    research_costs = [call.cost for call in calls if call.cost is not None]
    grading_cost = grading_call.cost if grading_call else None
    totals = [call.cost for call in every_call if call.cost is not None]
    return CostActual(
        research_usd=round(sum(research_costs), 6) if research_costs else None,
        grading_usd=grading_cost,
        total_usd=round(sum(totals), 6) if totals else None,
        research_prompt_tokens=_sum_optional([call.prompt_tokens for call in calls]),
        research_completion_tokens=_sum_optional([call.completion_tokens for call in calls]),
        grading_prompt_tokens=grading_call.prompt_tokens if grading_call else None,
        grading_completion_tokens=grading_call.completion_tokens if grading_call else None,
        billed_call_count=len(totals),
        failed_call_count=sum(1 for call in every_call if call.status == "failed"),
        unpriced_call_count=sum(1 for call in every_call if call.cost is None),
    )


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _research_prompt(brief: Brief, inputs: list[InputArtifact]) -> str:
    sections = [
        (
            "You are one independent research model in a multi-model commission. "
            "Answer the brief using only the supplied materials and clearly identify uncertainty."
        ),
        "BRIEF — SENT VERBATIM\n" + brief.verbatim(),
    ]
    for index, item in enumerate(inputs, start=1):
        if item.state != "excluded":
            sections.append(f"INPUT {index}: {item.name}\n---\n{item.text}\n---")
    return "\n\n".join(sections)


def _grading_prompt(calls: list[CallRecord], responding_models: list[str]) -> str:
    anonymized = []
    for index, model in enumerate(responding_models):
        call = next(call for call in calls if call.model_id == model)
        anonymized.append(f"MODEL {index + 1}\n---\n{call.body}\n---")
    return "\n\n".join(
        [
            (
                "Blindly compare these independent research outputs. Return JSON only with this shape: "
                '{"claims":[{"text":"one declarative proposition","scores":'
                '[{"model_index":1,"score":-3,"quote":"verbatim span"}]}],'
                '"report_markdown":"report with a What this run does not establish section"}. '
                "Scores are integers -3..3; 0 means no bearing statement and must have an empty quote. "
                "Every non-zero score must quote an exact verbatim span. Include the union of material claims. "
                "Do not identify or rank the model authors."
            ),
            *anonymized,
        ]
    )


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.DOTALL)
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise TypeError("analysis response is not a JSON object")
    return parsed


def _claims_and_scores(
    payload: dict[str, Any], calls: list[CallRecord], grading_call_id: str | None
) -> tuple[list[ClaimRecord], list[ScoreRecord]]:
    responding = [call.model_id for call in calls if call.status == "success"]
    failed = [call.model_id for call in calls if call.status == "failed"]
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise TypeError("analysis response has no claims list")
    claims: list[ClaimRecord] = []
    scores: list[ScoreRecord] = []
    for claim_index, raw_claim in enumerate(raw_claims, start=1):
        if not isinstance(raw_claim, dict) or not str(raw_claim.get("text") or "").strip():
            raise ValueError("analysis claim is missing declarative text")
        claim_id = f"c-{claim_index:03d}"
        rows = raw_claim.get("scores")
        rows = rows if isinstance(rows, list) else []
        by_index: dict[int, dict[str, Any]] = {}
        for candidate in rows:
            if not isinstance(candidate, dict):
                continue
            candidate_index = candidate.get("model_index")
            if isinstance(candidate_index, int):
                by_index[candidate_index] = candidate
        numeric_scores: list[int] = []
        for model_index, model_id in enumerate(responding, start=1):
            row = by_index.get(model_index, {})
            raw_score = row.get("score", 0)
            score = int(raw_score) if isinstance(raw_score, int | float | str) else 0
            if score < SCORE_MIN or score > SCORE_MAX:
                raise ValueError(
                    f"score outside {SCORE_MIN}..{SCORE_MAX} for {claim_id}/{model_id}"
                )
            quote = str(row.get("quote") or "").strip() or None
            if score != 0 and not quote:
                raise ValueError(f"non-zero score has no quote for {claim_id}/{model_id}")
            if score == 0:
                quote = None
            numeric_scores.append(score)
            scores.append(
                ScoreRecord(
                    claim_id=claim_id,
                    model_id=model_id,
                    score=score,
                    quote=quote,
                    grading_call_id=grading_call_id,
                )
            )
        scores.extend(
            ScoreRecord(
                claim_id=claim_id,
                model_id=model_id,
                score=None,
                quote=None,
                grading_call_id=None,
            )
            for model_id in failed
        )
        claims.append(
            ClaimRecord(
                claim_id=claim_id,
                text=str(raw_claim["text"]).strip(),
                group=classify_scores(numeric_scores, len(responding)),
                responding_model_count=len(responding),
            )
        )
    return claims, scores


class CommissionService:
    def __init__(self, config: Config, gateway: Gateway) -> None:
        self.config = config
        self.gateway = gateway
        self.store = RunStore(config.data_dir)

    async def create_draft(
        self,
        brief: Brief,
        inputs: list[InputArtifact],
        models: list[str],
        grading_model: str,
        ceiling_usd: float,
        web_search: bool = False,
        web_search_rationale: str = "",
    ) -> Draft:
        if not brief.task.strip():
            raise CommissionError("The Task field is required.")
        web_search_rationale = web_search_rationale.strip()
        if web_search and not web_search_rationale:
            # Search is the most expensive thing this system can be asked to
            # do, and the cost is invisible at the point of asking: results
            # bill as prompt tokens, so the same brief cost $0.75 without it
            # and $3.03 with it. It also costs the run its reproducibility.
            # Refusing without a reason is not bureaucracy -- it is the only
            # moment anyone is required to think about whether this brief
            # actually needs live sources, or whether training data answers it.
            raise CommissionError(
                "Web search needs a reason. It quadrupled the bill on the one brief "
                "measured both ways ($0.75 -> $3.03, because results are billed as "
                "prompt tokens) and makes the run unreproducible from its inputs. "
                "Say what this brief needs live sources for -- recency, a specific "
                "document, a claim training data cannot settle -- or leave it off."
            )
        models = list(dict.fromkeys(model.strip() for model in models if model.strip()))
        if len(models) < 2:
            raise CommissionError("Select at least two independent research models.")
        input_tokens = max(
            1, (sum(item.extracted_chars for item in inputs) + len(brief.verbatim())) // 4
        )
        estimate: float | None = None
        estimate_detail: CostEstimate | None = None
        pricing_error: str | None = None
        grading_input_tokens = input_tokens + ASSUMED_COMPLETION_TOKENS * len(models)
        try:
            research_estimate = await self.gateway.estimate(
                models, input_tokens, web_search=web_search
            )
            # Grading reads the research bodies; it never searches.
            grading_estimate = await self.gateway.estimate([grading_model], grading_input_tokens)
            # estimate() folds search into its total; split it back out rather
            # than pricing twice, so the operator can see which term is which.
            search_estimate = WEB_SEARCH_COST_USD * len(models) if web_search else 0.0
            estimate = round(research_estimate + grading_estimate, 6)
            # The bound, priced the same way but assuming every model writes
            # to the cap. This is what the ceiling is checked against: the
            # estimate above is a prediction and has been wrong by 2.8x, so
            # enforcing a ceiling against it enforces nothing (#49).
            worst_case = round(
                await self.gateway.estimate(
                    models,
                    input_tokens,
                    web_search=web_search,
                    completion_tokens=MAX_COMPLETION_TOKENS,
                )
                + await self.gateway.estimate(
                    [grading_model],
                    input_tokens + MAX_COMPLETION_TOKENS * len(models),
                    completion_tokens=MAX_COMPLETION_TOKENS,
                ),
                6,
            )
            estimate_detail = CostEstimate(
                research_usd=round(research_estimate - search_estimate, 6),
                grading_usd=round(grading_estimate, 6),
                web_search_usd=round(search_estimate, 6),
                total_usd=estimate,
                input_tokens=input_tokens,
                grading_input_tokens=grading_input_tokens,
                assumed_completion_tokens=ASSUMED_COMPLETION_TOKENS,
                research_model_count=len(models),
                worst_case_usd=worst_case,
            )
        except (httpx.HTTPError, CommissionError, ValueError) as exc:
            pricing_error = str(exc)
        draft = Draft(
            draft_id=f"d-{uuid.uuid4().hex[:12]}",
            created_at=datetime.now(UTC),
            brief=brief,
            inputs=inputs,
            models=models,
            grading_model=grading_model,
            ceiling_usd=ceiling_usd,
            estimate_usd=estimate,
            estimate_detail=estimate_detail,
            pricing_error=pricing_error,
            web_search=web_search,
            web_search_rationale=web_search_rationale,
        )
        self.store.save_draft(draft)
        return draft

    async def dispatch(self, draft_id: str) -> RunRecord:
        draft = self.store.load_draft(draft_id)
        # The ceiling is checked against the BOUND, not the prediction. A
        # ceiling compared only against an estimate stops nothing: r-2026-0812-03
        # was estimated at $0.2873, passed a $1.00 ceiling, and cost $3.0321
        # (#49). If the worst case does not fit, the honest answer is to refuse
        # before spending anything and let the operator set a real number.
        worst_case = draft.estimate_detail.worst_case_usd if draft.estimate_detail else None
        if worst_case is not None and worst_case > draft.ceiling_usd:
            raise CommissionError(
                f"This run can cost up to ${worst_case:.4f} if every model writes to the "
                f"{MAX_COMPLETION_TOKENS:,}-token cap, which exceeds the ${draft.ceiling_usd:.4f} "
                f"ceiling. The estimate is ${draft.estimate_usd:.4f}, but an estimate is not a "
                "cap. Raise the ceiling deliberately, drop a model, or turn web search off."
            )
        # No live pricing means no bound could be computed; the estimate is all
        # there is to check against, which is weaker and should say so.
        if (
            worst_case is None
            and draft.estimate_usd is not None
            and draft.estimate_usd > draft.ceiling_usd
        ):
            raise CommissionError(
                f"Estimate ${draft.estimate_usd:.4f} exceeds ceiling ${draft.ceiling_usd:.4f}."
            )
        started = datetime.now(UTC)
        run_id = self.store.next_run_id(started)
        brief_text = draft.brief.verbatim()
        run = RunRecord(
            run_id=run_id,
            brief_revision="A",
            brief_sha256=hashlib.sha256(brief_text.encode()).hexdigest(),
            status="running",
            created_at=started,
            cost_estimate=draft.estimate_usd,
            inputs=draft.inputs,
            dispatched_models=draft.models,
            grading_model=draft.grading_model,
            web_search=draft.web_search,
        )
        run_dir = self.store.run_dir(run_id)
        self.store.write_run(run)
        _write_atomic(run_dir / "brief.md", (brief_text + "\n").encode())
        for item in draft.inputs:
            if item.state != "excluded":
                original_suffix = Path(item.name).suffix.lower() or ".bin"
                _write_atomic(
                    run_dir / "inputs" / "original" / f"{item.sha256}{original_suffix}",
                    base64.b64decode(item.original_base64),
                )
                _write_atomic(
                    run_dir / "inputs" / "extracted" / f"{item.sha256}.txt",
                    item.text.encode(),
                )

        prompt = _research_prompt(draft.brief, draft.inputs)
        calls = await asyncio.gather(
            *(
                self.gateway.complete(model, prompt, web_search=draft.web_search)
                for model in draft.models
            )
        )
        for call in calls:
            _write_atomic(
                run_dir / "raw" / f"{_safe_model_name(call.model_id)}.json",
                _json_bytes(call.model_dump(mode="json")),
            )

        successful = [call for call in calls if call.status == "success"]
        limitations = [
            f"{call.model_id} failed; its claim cells are missing observations (✕), not silence."
            for call in calls
            if call.status == "failed"
        ]
        limitations += [
            f"{call.model_id} was cut off at the {MAX_COMPLETION_TOKENS:,}-token cap; "
            "its answer is incomplete and its silence on a claim may be the cap talking."
            for call in calls
            if call.truncated
        ]
        if draft.web_search:
            # brief_sha256 pins the question, not the answer: live sources move.
            limitations.append(
                f"Research calls searched the live web on {started.date().isoformat()}; "
                "re-running this brief will read whatever the sources say then, not now."
            )
        claims: list[ClaimRecord] = []
        scores: list[ScoreRecord] = []
        grading_call: CallRecord | None = None
        report = "# Result\n\n## What this run does not establish\n\nModel agreement is not verification.\n"
        # The research calls are away and billed; grading is the one decision
        # still open, so it is where a ceiling can still bite. Web search makes
        # the modelled worst case unreliable on the input side -- results arrive
        # as prompt tokens nobody bounded -- so check what was actually spent
        # rather than trusting the bound that let this run start.
        spent = sum(call.cost or 0.0 for call in calls)
        if successful and spent >= draft.ceiling_usd:
            limitations.append(
                f"The grading call was not made: research already spent ${spent:.4f} of the "
                f"${draft.ceiling_usd:.4f} ceiling. The research outputs are preserved and can "
                "be graded by re-running with a higher ceiling; no claim landscape exists yet."
            )
            successful = []
        if successful:
            grading_call = await self.gateway.complete(
                draft.grading_model,
                _grading_prompt(calls, [call.model_id for call in successful]),
            )
            _write_atomic(
                run_dir / "raw" / "grading.json",
                _json_bytes(grading_call.model_dump(mode="json")),
            )
            if grading_call.truncated:
                # Named separately from the parse error it will almost
                # certainly cause: "the grader ran out of room" and "the
                # grader emitted bad JSON" call for different fixes.
                limitations.append(
                    f"The grading call was cut off at the {MAX_COMPLETION_TOKENS:,}-token cap; "
                    "a claim landscape parsed from it, if any, is incomplete."
                )
            if grading_call.status == "success" and grading_call.body:
                try:
                    analysis = _parse_json_object(grading_call.body)
                    claims, scores = _claims_and_scores(analysis, calls, grading_call.generation_id)
                    report = str(analysis.get("report_markdown") or report)
                except (ValueError, TypeError, KeyError) as exc:
                    limitations.append(f"Grading output could not be validated: {exc}")
            else:
                limitations.append("The grading call failed; no claim landscape is available.")
        else:
            limitations.append("Every research call failed; no claim landscape is available.")

        _write_atomic(
            run_dir / "claims.json", _json_bytes([claim.model_dump() for claim in claims])
        )
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(
            csv_buffer,
            fieldnames=["claim_id", "model_id", "score", "quote", "grading_call_id"],
        )
        writer.writeheader()
        for score in scores:
            writer.writerow(score.model_dump())
        _write_atomic(run_dir / "scores.csv", csv_buffer.getvalue().encode())
        _write_atomic(run_dir / "report.md", (report.rstrip() + "\n").encode())

        completed = datetime.now(UTC)
        actual = _cost_actual(calls, grading_call)
        # Predicted and measured, side by side and per component. One run cannot
        # correct the estimate; a corpus of these can (dhk/alexandria#32).
        _write_atomic(
            run_dir / "cost.json",
            _json_bytes(
                {
                    "run_id": run_id,
                    "estimate": draft.estimate_detail.model_dump(mode="json")
                    if draft.estimate_detail
                    else None,
                    "estimate_total_usd": draft.estimate_usd,
                    "actual": actual.model_dump(mode="json"),
                    "web_search": draft.web_search,
                    "dispatched_models": draft.models,
                    "grading_model": draft.grading_model,
                }
            ),
        )
        known_costs = [
            call.cost for call in [*calls, grading_call] if call and call.cost is not None
        ]
        grading_ok = grading_call is not None and grading_call.status == "success" and bool(claims)
        if not successful:
            status = "failed"
        elif len(successful) != len(calls) or not grading_ok:
            status = "partial"
        else:
            status = "completed"
        run = run.model_copy(
            update={
                "status": status,
                "completed_at": completed,
                "elapsed_seconds": round((completed - started).total_seconds(), 3),
                "cost_actual": round(sum(known_costs), 6) if known_costs else None,
                "limitations": limitations,
            }
        )
        self.store.write_run(run)
        manifest = {
            "run_id": run_id,
            "brief_revision": run.brief_revision,
            "brief_sha256": run.brief_sha256,
            "grading_model": draft.grading_model,
            "resolved_model_ids": {
                call.model_id: call.resolved_model_id for call in calls if call.resolved_model_id
            },
            "generation_ids": [
                call.generation_id for call in [*calls, grading_call] if call and call.generation_id
            ],
            "extraction_method": {item.name: item.extraction_method for item in draft.inputs},
            "redispatched_models": [],
            "artifacts": [
                "run.json",
                "brief.md",
                "claims.json",
                "scores.csv",
                "report.md",
                "cost.json",
                "manifest.json",
                "raw/",
                "inputs/",
            ],
        }
        _write_atomic(run_dir / "manifest.json", _json_bytes(manifest))
        return run
