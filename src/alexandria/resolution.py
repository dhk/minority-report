"""The resolution taxonomy (issue #35): an idea's "no dead ends" outcome.

Every idea eventually resolves to exactly one of three terminal outcomes --
``implemented``, ``morphed``, or ``nixed``. There is no fourth "back-burnered"
value: an idea with nothing recorded yet is simply *unresolved* (the absence
of a ``resolution.yaml``), never a stored state. ``morphed`` without a
forward pointer to what the idea became is itself a dead end, so it's the
one outcome this module refuses to accept without ``expression`` set.

Consistent with every other MCP tool in this server (``begin_research`` /
``run_research``), nothing here writes to the git-tracked ``research/``
system of record. ``research/`` only changes by a deliberate operator
commit (DESIGN.md). This module validates and renders the YAML text; saving
and committing it is the operator's action, not this server's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, model_validator

ResolutionOutcome = Literal["implemented", "morphed", "nixed"]

RESOLUTION_FILENAME = "resolution.yaml"


class ResolutionError(Exception):
    """A resolution.yaml exists but violates the taxonomy's own contract."""


class Resolution(BaseModel):
    outcome: ResolutionOutcome
    expression: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    rationale: str | None = None

    @model_validator(mode="after")
    def _morphed_requires_expression(self) -> Resolution:
        if self.outcome == "morphed" and not (self.expression or "").strip():
            raise ValueError(
                "outcome 'morphed' requires expression (a forward pointer to what the "
                "idea became) -- morphed without a pointer is itself a dead end"
            )
        return self

    def to_yaml(self) -> str:
        data = {
            "outcome": self.outcome,
            **{
                key: value
                for key, value in (
                    ("expression", self.expression),
                    ("decided_by", self.decided_by),
                    ("decided_at", self.decided_at),
                    ("rationale", self.rationale),
                )
                if value
            },
        }
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def parse_resolution_yaml(path: Path) -> Resolution | None:
    """Reads and validates an investigation's resolution.yaml.

    Returns None when the file is absent -- that's "unresolved," the
    expected, ordinary state for an idea still in flight, not an error.
    Raises ResolutionError if the file exists but violates the taxonomy
    (an unknown outcome value, or 'morphed' without 'expression').
    """
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ResolutionError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ResolutionError(f"{path} must be a YAML mapping.")
    try:
        return Resolution.model_validate(raw)
    except ValidationError as exc:
        raise ResolutionError(f"{path} does not satisfy the resolution taxonomy: {exc}") from exc


def draft_resolution(
    *,
    outcome: str,
    expression: str = "",
    decided_by: str = "",
    decided_at: str = "",
    rationale: str = "",
) -> Resolution:
    """Validates a proposed resolution without writing anything.

    Raises ResolutionError (never a bare pydantic ValidationError) so callers
    -- the MCP tool, tests, anything else -- get one exception type to catch.
    """
    try:
        return Resolution.model_validate(
            {
                "outcome": outcome,
                "expression": expression or None,
                "decided_by": decided_by or None,
                "decided_at": decided_at or None,
                "rationale": rationale or None,
            }
        )
    except ValidationError as exc:
        raise ResolutionError(str(exc)) from exc
