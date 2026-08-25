"""Typed records shared by the commission service, resolver, and web surface."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

InputState = Literal["extracted", "warning", "excluded"]
RunStatus = Literal["completed", "partial", "failed", "draft", "running"]
ClaimGroup = Literal["consensus", "disagreement", "novel", "thin", "silent"]


class InputArtifact(BaseModel):
    name: str
    format: str
    bytes: int
    extracted_chars: int
    encoding: str | None
    sha256: str
    state: InputState
    warning: str | None = None
    source_url: str | None = None
    extraction_method: str
    text: str = Field(default="", repr=False)
    original_base64: str = Field(default="", repr=False)


class Brief(BaseModel):
    task: str
    context: str = ""
    constraints: str = ""
    output_needs: str = ""

    def verbatim(self) -> str:
        return (
            f"Task\n{self.task}\n\nContext\n{self.context}\n\n"
            f"Constraints\n{self.constraints}\n\nOutput needs\n{self.output_needs}"
        )


class CostEstimate(BaseModel):
    """What the estimate is made of, and what it assumed.

    Kept as components rather than one number because a run that comes in
    over its estimate is otherwise undiagnosable: you can see that the total
    was wrong but not which term was wrong. Recording the assumptions
    alongside the prediction is what makes the formula fittable later
    (dhk/alexandria#32).
    """

    research_usd: float
    grading_usd: float
    web_search_usd: float
    total_usd: float
    input_tokens: int
    grading_input_tokens: int
    assumed_completion_tokens: int
    research_model_count: int
    #: What this run cannot exceed if every model writes to the completion cap:
    #: the number the ceiling is actually checked against. The estimate above
    #: is a prediction and has been wrong by 2.8x on a measured run; this is a
    #: bound. None when live pricing could not be read.
    worst_case_usd: float | None = None


class CostActual(BaseModel):
    """What the run really cost, in the same shape as the estimate."""

    research_usd: float | None
    grading_usd: float | None
    total_usd: float | None
    research_prompt_tokens: int | None
    research_completion_tokens: int | None
    grading_prompt_tokens: int | None
    grading_completion_tokens: int | None
    billed_call_count: int
    failed_call_count: int
    unpriced_call_count: int


class Draft(BaseModel):
    draft_id: str
    created_at: datetime
    brief: Brief
    inputs: list[InputArtifact]
    models: list[str]
    grading_model: str
    ceiling_usd: float
    estimate_usd: float | None
    estimate_detail: CostEstimate | None = None
    pricing_error: str | None = None
    #: Off by default. Live search quadrupled the bill on the one brief where
    #: both were measured ($0.75 -> $3.03), because results arrive as prompt
    #: tokens, and it costs the run its reproducibility. Turning it on is a
    #: decision, so it has to be made explicitly and said out loud below.
    web_search: bool = False
    #: Why this brief needs live sources. Required when web_search is on, and
    #: shown in the review so the operator approves the reason, not just the
    #: flag.
    web_search_rationale: str = ""


class SourceCitation(BaseModel):
    url: str
    title: str | None = None
    content: str | None = None


class CallRecord(BaseModel):
    model_id: str
    resolved_model_id: str | None = None
    status: Literal["success", "failed"]
    body: str | None = None
    raw_response: str | None = None
    generation_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost: float | None = None
    latency_ms: int
    error: str | None = None
    status_code: int | None = None
    #: The provider stopped at max_tokens rather than finishing the answer
    #: (finish_reason == "length"). The body is kept -- a truncated answer is
    #: a partial observation, not an absent one -- but a run must be able to
    #: say which of its answers were cut off.
    truncated: bool = False
    citations: list[SourceCitation] = Field(default_factory=list)


#: docs/confidence-calibration.md §4. The grader emits the pair; the integer is
#: a fixed lookup, so a mapping change can be applied to stored runs instead of
#: re-dispatching them.
Stance = Literal["supports", "disputes", "silent"]
Strength = Literal["strong", "moderate", "weak"]


class ScoreRecord(BaseModel):
    claim_id: str
    model_id: str
    #: None only where the model returned no output at all: a failed research
    #: call has no stance, and must not be coerced to silent.
    stance: Stance | None = None
    #: None when the stance is silent, which takes no strength, and when there
    #: is no stance to qualify.
    strength: Strength | None = None
    score: int | None
    quote: str | None
    grading_call_id: str | None


class ClaimRecord(BaseModel):
    claim_id: str
    text: str
    group: ClaimGroup
    responding_model_count: int


class Instrument(BaseModel):
    """What apparatus produced a run's claim landscape.

    Facts only. Whether the run conforms to the calibration spec is derived
    from these at read time and deliberately not stored — the same rule the
    spec applies to a score, and for the same reason: a stored `conforming:
    true` is a value someone chose rather than a value anyone can check.

    Written by the code that did the grading. Until now the corpus carried this
    block only because a person added it at promotion time, which meant a run
    could be graded one way and described another.
    """

    spec_version: Literal["confidence-calibration/draft-v1"] = "confidence-calibration/draft-v1"
    grader_topology: Literal["per-model-blind", "single-call-all-models"]
    score_derivation: Literal["derived-lookup", "model-assigned"]
    extraction_pass: Literal["separate", "fused"]
    note: str | None = None


class RunRecord(BaseModel):
    run_id: str
    brief_revision: str
    brief_sha256: str
    status: RunStatus
    created_at: datetime
    completed_at: datetime | None = None
    cost_estimate: float | None = None
    cost_actual: float | None = None
    elapsed_seconds: float | None = None
    inputs: list[InputArtifact]
    dispatched_models: list[str]
    grading_model: str
    web_search: bool = False
    instrument: Instrument | None = None
    limitations: list[str] = Field(default_factory=list)
