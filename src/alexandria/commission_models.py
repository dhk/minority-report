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


class Draft(BaseModel):
    draft_id: str
    created_at: datetime
    brief: Brief
    inputs: list[InputArtifact]
    models: list[str]
    grading_model: str
    ceiling_usd: float
    estimate_usd: float | None
    pricing_error: str | None = None
    web_search: bool = True


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


class ScoreRecord(BaseModel):
    claim_id: str
    model_id: str
    score: int | None
    quote: str | None
    grading_call_id: str | None


class ClaimRecord(BaseModel):
    claim_id: str
    text: str
    group: ClaimGroup
    responding_model_count: int


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
    limitations: list[str] = Field(default_factory=list)
