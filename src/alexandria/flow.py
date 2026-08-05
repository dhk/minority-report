"""Assembles the idea-to-expression flow document (RFC-0007) for one investigation.

This is the "analysis layer" RFC-0007 SS08 assumes exists. It reads only what is
already committed under an investigation's lifecycle directories
(``research_repo.LIFECYCLE_STAGES``) plus two small new artifact conventions this
module introduces to close the gaps RFC-0007 SS12 flagged as unresolved:

- ``topic.yaml`` gains four optional fields (``origin``, ``claim_under_test``,
  ``why_now``, ``scope``) read for the Idea stage's four lanes. Absent fields
  render "Not recorded" -- this module never invents lane prose (RFC-0007's
  non-goals: "No summarisation by the UI").
- ``resolution.yaml`` at the investigation root is read and validated by
  ``alexandria.resolution`` (issue #35's taxonomy: ``implemented`` /
  ``morphed`` / ``nixed``, or the file absent entirely for "unresolved").
  This module only renders what that module hands back.

Nothing here writes to the repository. This module is read-only, same discipline
as research_repo.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from alexandria.infrastructure.config import Config
from alexandria.infrastructure.research_repo import Investigation, find_investigation
from alexandria.resolution import (
    RESOLUTION_FILENAME,
    Resolution,
    ResolutionError,
    parse_resolution_yaml,
)

# RFC-0007 SS03's six stages, in fixed display order, mapped onto the lifecycle
# directories they read from. Not every LIFECYCLE_STAGES entry is shown -- the
# flow is a compressed six-lane view over the repository's nine-stage lifecycle.
_STAGE_KEYS = ("idea", "brief", "run", "results", "synthesis", "resolution")

_STAGE_LABELS = {
    "idea": "Idea",
    "brief": "Research brief",
    "run": "Engine run",
    "results": "Results",
    "synthesis": "Synthesis",
    "resolution": "Resolution",
}

_LANE_LABELS = {
    "idea": ("Origin", "Claim under test", "Why now", "Scope"),
    "brief": ("Task", "Context", "Constraints", "Output needs"),
    "run": ("Models dispatched", "Inputs as sent", "Spend", "Failures"),
    "results": ("Consensus", "Disagreement", "Novel", "Coverage"),
    "synthesis": ("Findings", "Evidence base", "Not established", "Open questions"),
    "resolution": ("Outcome", "Expression", "Decided by & when", "Rationale"),
}

_NOT_RECORDED = "Not recorded"
_LANE_SUMMARY_LIMIT = 160


class FlowDataError(Exception):
    """A flow-relevant artifact exists but could not be read (e.g. invalid YAML/JSON)."""


@dataclass(frozen=True)
class Lane:
    label: str
    summary: str
    """Always a string -- ``_NOT_RECORDED`` stands in for an absent value.
    RFC-0007 SS03: "Four lanes always render."
    """
    count: int | None = None
    accent: str | None = None  # "orange" | "purple" | "teal" | None


@dataclass(frozen=True)
class Excerpt:
    paragraphs: tuple[str, ...]
    shown_of_total: tuple[int, int]
    artifact_path: str
    sha256: str | None


@dataclass(frozen=True)
class StageRecord:
    key: str
    label: str
    state: str  # "present" | "abandoned" | "not_reached"
    headline: str
    meta: tuple[str, ...]
    lanes: tuple[Lane, Lane, Lane, Lane]
    excerpt: Excerpt | None
    superseded_count: int = 0
    failed_calls: int = 0  # only meaningful for "run"; drives SS07's failure banner


@dataclass(frozen=True)
class FlowDocument:
    idea_slug: str
    title: str
    opened: str | None
    stages: tuple[StageRecord, ...]  # exactly six, SS08 order
    resolution: Resolution | None  # None = unresolved (issue #35's taxonomy)


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def _truncate(text: str, limit: int = _LANE_SUMMARY_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit + 1].rsplit(" ", maxsplit=1)[0] + "…"


def _paragraphs(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip())


def _read_yaml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise FlowDataError(f"{path} is not valid YAML: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlowDataError(f"{path} is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _lane(label: str, value: object, *, accent: str | None = None) -> Lane:
    if value is None or (isinstance(value, str) and not value.strip()):
        return Lane(label=label, summary=_NOT_RECORDED)
    if isinstance(value, list):
        text = ", ".join(str(v) for v in value) if value else ""
        return Lane(
            label=label,
            summary=_truncate(text) if text else _NOT_RECORDED,
            count=len(value),
            accent=accent,
        )
    return Lane(label=label, summary=_truncate(str(value)), accent=accent)


def _excerpt_from_file(path: Path, *, shown: int = 2) -> Excerpt | None:
    if not path.is_file():
        return None
    import hashlib

    raw = path.read_bytes()
    paragraphs = _paragraphs(raw.decode("utf-8"))
    return Excerpt(
        paragraphs=paragraphs[:shown],
        shown_of_total=(min(shown, len(paragraphs)), len(paragraphs)),
        artifact_path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _brief_sections(text: str) -> dict[str, str]:
    """Parses the verbatim format Brief.verbatim() writes (also used for 01-brief/brief.md)."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        header = line.strip()
        if header in {"Task", "Context", "Constraints", "Output needs"}:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = header
            buffer = []
        else:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _idea_stage(investigation_dir: Path, topic: dict[str, object]) -> StageRecord:
    lanes = (
        _lane("Origin", topic.get("origin")),
        _lane("Claim under test", topic.get("claim_under_test")),
        _lane("Why now", topic.get("why_now")),
        _lane("Scope", topic.get("scope")),
    )
    title = str(topic.get("title") or "Untitled idea")
    meta = []
    if topic.get("opened"):
        meta.append(f"opened {topic['opened']}")
    if topic.get("assurance_level"):
        meta.append(str(topic["assurance_level"]).upper())
    excerpt = _excerpt_from_file(investigation_dir / "00-topic" / "note.md")
    return StageRecord(
        key="idea",
        label=_STAGE_LABELS["idea"],
        state="present",
        headline=title,
        meta=tuple(meta),
        lanes=lanes,
        excerpt=excerpt,
    )


_NOT_TASK_SHAPED = "Not structured as Task/Context/Constraints/Output needs — see the full brief."


def _freeform_brief_headline(text: str) -> str:
    """Falls back to the document's own title when it isn't in the commission
    surface's verbatim Task/Context/Constraints/Output-needs format -- a real
    investigation's brief (freeform markdown, written by a human or an
    interactive session rather than Brief.verbatim()) is a second, equally
    legitimate brief shape this module has to read, not a malformed one.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "Brief on file"


def _brief_stage(investigation_dir: Path) -> StageRecord:
    brief_path = investigation_dir / "01-brief" / "brief.md"
    if not brief_path.is_file():
        return _empty_stage("brief")
    text = brief_path.read_text(encoding="utf-8")
    sections = _brief_sections(text)
    if sections:
        lanes = (
            _lane("Task", sections.get("Task")),
            _lane("Context", sections.get("Context")),
            _lane("Constraints", sections.get("Constraints")),
            _lane("Output needs", sections.get("Output needs")),
        )
        headline = _truncate(sections.get("Task") or "Brief on file", limit=112)
    else:
        # Present, real, substantial content -- just not in the one format
        # this module knows how to slot into four lanes. "Not recorded"
        # would misstate that as absence; say what's actually true instead.
        lanes = (
            Lane("Task", _NOT_TASK_SHAPED),
            Lane("Context", _NOT_TASK_SHAPED),
            Lane("Constraints", _NOT_TASK_SHAPED),
            Lane("Output needs", _NOT_TASK_SHAPED),
        )
        headline = _truncate(_freeform_brief_headline(text), limit=112)
    excerpt = _excerpt_from_file(brief_path)
    return StageRecord(
        key="brief",
        label=_STAGE_LABELS["brief"],
        state="present",
        headline=headline,
        meta=(),
        lanes=lanes,
        excerpt=excerpt,
    )


def _run_stage(investigation_dir: Path) -> StageRecord:
    runs_dir = investigation_dir / "03-runs"
    manifest = _read_json(runs_dir / "manifest.json")
    if not manifest:
        return _empty_stage("run")
    models = manifest.get("models_dispatched") or []
    inputs = manifest.get("inputs") or []
    spend = manifest.get("spend_usd")
    failures = manifest.get("failures") or []
    failed_count = len(failures) if isinstance(failures, list) else _as_int(failures)
    lanes = (
        _lane("Models dispatched", models),
        _lane("Inputs as sent", inputs),
        _lane("Spend", f"${spend:.4f}" if isinstance(spend, int | float) else None),
        _lane("Failures", failures, accent="orange" if failed_count else None),
    )
    meta = [f"{len(models) if isinstance(models, list) else 0} models dispatched"]
    if failed_count:
        noun = "call" if failed_count == 1 else "calls"
        meta.append(f"{failed_count} failed {noun}")
    superseded = _as_int(manifest.get("superseded_count"))
    excerpt = (
        _excerpt_from_file(runs_dir / "manifest.json")
        if not manifest.get("manifest_excerpt")
        else None
    )
    manifest_excerpt = manifest.get("manifest_excerpt")
    if isinstance(manifest_excerpt, str) and manifest_excerpt.strip():
        paras = _paragraphs(manifest_excerpt)
        excerpt = Excerpt(
            paragraphs=paras[:2],
            shown_of_total=(min(2, len(paras)), len(paras)),
            artifact_path=str(runs_dir / "manifest.json"),
            sha256=None,
        )
    return StageRecord(
        key="run",
        label=_STAGE_LABELS["run"],
        state="present",
        headline=f"{len(models) if isinstance(models, list) else 0} models dispatched"
        + (f", {failed_count} failed" if failed_count else ""),
        meta=tuple(meta),
        lanes=lanes,
        excerpt=excerpt,
        superseded_count=superseded,
        failed_calls=failed_count,
    )


def _results_stage(investigation_dir: Path) -> StageRecord:
    analysis_dir = investigation_dir / "05-analysis"
    summary = _read_yaml(analysis_dir / "summary.yaml")
    report_path = analysis_dir / "report.md"
    if not summary and not report_path.is_file():
        return _empty_stage("results")
    lanes = (
        _lane("Consensus", summary.get("consensus")),
        _lane("Disagreement", summary.get("disagreement"), accent="orange"),
        _lane("Novel", summary.get("novel"), accent="purple"),
        _lane("Coverage", summary.get("coverage"), accent="teal"),
    )
    headline = str(summary.get("headline") or "Results on file")
    excerpt = _excerpt_from_file(report_path)
    return StageRecord(
        key="results",
        label=_STAGE_LABELS["results"],
        state="present",
        headline=_truncate(headline, limit=112),
        meta=(),
        lanes=lanes,
        excerpt=excerpt,
    )


def _synthesis_stage(investigation_dir: Path) -> StageRecord:
    synth_dir = investigation_dir / "06-synthesis"
    summary = _read_yaml(synth_dir / "summary.yaml")
    synth_path = synth_dir / "synthesis.md"
    if not summary and not synth_path.is_file():
        return _empty_stage("synthesis")
    lanes = (
        _lane("Findings", summary.get("findings")),
        _lane("Evidence base", summary.get("evidence_base")),
        _lane("Not established", summary.get("not_established"), accent="orange"),
        _lane("Open questions", summary.get("open_questions")),
    )
    headline = str(summary.get("headline") or "Synthesis on file")
    excerpt = _excerpt_from_file(synth_path)
    return StageRecord(
        key="synthesis",
        label=_STAGE_LABELS["synthesis"],
        state="present",
        headline=_truncate(headline, limit=112),
        meta=(),
        lanes=lanes,
        excerpt=excerpt,
    )


def _resolution_stage(investigation_dir: Path, resolution: Resolution | None) -> StageRecord:
    if resolution is None:
        lanes = (
            Lane("Outcome", "UNRESOLVED"),
            _lane("Expression", None),
            _lane("Decided by & when", None),
            _lane("Rationale", None),
        )
        headline = "Unresolved"
    else:
        lanes = (
            Lane("Outcome", resolution.outcome),
            _lane("Expression", resolution.expression),
            _lane(
                "Decided by & when",
                f"{resolution.decided_by} · {resolution.decided_at}"
                if resolution.decided_by or resolution.decided_at
                else None,
            ),
            _lane("Rationale", resolution.rationale),
        )
        headline = resolution.outcome
    published_dir = investigation_dir / "08-published"
    excerpt = None
    if published_dir.is_dir():
        for candidate in sorted(published_dir.glob("*.md")):
            excerpt = _excerpt_from_file(candidate)
            break
    if excerpt is None and resolution is not None and resolution.rationale:
        paras = _paragraphs(resolution.rationale)
        excerpt = Excerpt(
            paragraphs=paras[:2],
            shown_of_total=(min(2, len(paras)), len(paras)),
            artifact_path=str(investigation_dir / RESOLUTION_FILENAME),
            sha256=None,
        )
    return StageRecord(
        key="resolution",
        label=_STAGE_LABELS["resolution"],
        state="present",
        headline=headline,
        meta=(),
        lanes=lanes,
        excerpt=excerpt,
    )


def _empty_stage(key: str) -> StageRecord:
    """A stage with nothing on disk. Caller fixes up state (not_reached/abandoned)."""
    lanes = tuple(Lane(label=label, summary=_NOT_RECORDED) for label in _LANE_LABELS[key])
    return StageRecord(
        key=key,
        label=_STAGE_LABELS[key],
        state="not_reached",
        headline="",
        meta=(),
        lanes=lanes,  # type: ignore[arg-type]
        excerpt=None,
    )


def _read_resolution(investigation_dir: Path) -> Resolution | None:
    """None means unresolved (no resolution.yaml) -- the ordinary, expected
    state for an idea still in flight. Raises FlowDataError if the file
    exists but violates the taxonomy (issue #35's own module owns that
    validation; this just surfaces it as flow.py's one exception type).
    """
    try:
        return parse_resolution_yaml(investigation_dir / RESOLUTION_FILENAME)
    except ResolutionError as exc:
        raise FlowDataError(str(exc)) from exc


def _apply_reach_states(stages: list[StageRecord]) -> list[StageRecord]:
    """RFC-0007 SS07: a stage with nothing on disk is "not_reached" only if
    every later stage is also empty; otherwise it's "abandoned" (reached,
    produced nothing, but the arc continued past it).
    """
    has_content = [
        bool(s.headline) or s.excerpt is not None or s.state == "present" for s in stages
    ]
    # _resolution_stage always sets state="present" (even when outcome is None,
    # "unresolved" is itself a recorded state) -- so resolution only reads as
    # not_reached/abandoned when it truly has no resolution.yaml AND no
    # published piece AND synthesis never happened either.
    fixed: list[StageRecord] = []
    for i, stage in enumerate(stages):
        if stage.state == "present":
            fixed.append(stage)
            continue
        if any(has_content[i + 1 :]):
            fixed.append(_replace_state(stage, "abandoned"))
        else:
            fixed.append(_replace_state(stage, "not_reached"))
    return fixed


def _replace_state(stage: StageRecord, state: str) -> StageRecord:
    return StageRecord(
        key=stage.key,
        label=stage.label,
        state=state,
        headline=stage.headline,
        meta=stage.meta,
        lanes=stage.lanes,
        excerpt=stage.excerpt,
        superseded_count=stage.superseded_count,
        failed_calls=stage.failed_calls,
    )


def build_flow_document(config: Config, slug: str) -> FlowDocument | None:
    """Assembles the flow document for one investigation, or None if the slug
    does not exist under research/.
    """
    investigation = find_investigation(config, slug)
    if investigation is None:
        return None
    return _build(investigation)


def _build(investigation: Investigation) -> FlowDocument:
    investigation_dir = investigation.path
    topic = _read_yaml(investigation_dir / "topic.yaml")
    resolution = _read_resolution(investigation_dir)

    idea = _idea_stage(investigation_dir, topic)
    brief = _brief_stage(investigation_dir)
    run = _run_stage(investigation_dir)
    results = _results_stage(investigation_dir)
    synthesis = _synthesis_stage(investigation_dir)

    # Resolution reads as reached ("present", possibly with outcome=None ->
    # UNRESOLVED) only once synthesis actually happened -- otherwise there is
    # nothing to resolve and it should read not_reached like its neighbours.
    if synthesis.state == "present" or resolution is not None:
        stage_resolution = _resolution_stage(investigation_dir, resolution)
    else:
        stage_resolution = _empty_stage("resolution")

    stages = _apply_reach_states([idea, brief, run, results, synthesis, stage_resolution])

    return FlowDocument(
        idea_slug=investigation.slug,
        title=investigation.title or investigation.slug,
        opened=str(topic.get("opened")) if topic.get("opened") else None,
        stages=tuple(stages),
        resolution=resolution,
    )


def _lane_json(lane: Lane) -> dict[str, object]:
    return {
        "label": lane.label,
        "summary": lane.summary,
        "count": lane.count,
        "accent": lane.accent,
    }


def _excerpt_json(excerpt: Excerpt | None) -> dict[str, object] | None:
    if excerpt is None:
        return None
    return {
        "paragraphs": list(excerpt.paragraphs),
        "shown": excerpt.shown_of_total[0],
        "total": excerpt.shown_of_total[1],
        "artifact_path": excerpt.artifact_path,
        "sha256": excerpt.sha256,
    }


def _stage_json(stage: StageRecord) -> dict[str, object]:
    return {
        "key": stage.key,
        "label": stage.label,
        "state": stage.state,
        "headline": stage.headline,
        "meta": list(stage.meta),
        "lanes": [_lane_json(lane) for lane in stage.lanes],
        "excerpt": _excerpt_json(stage.excerpt),
        "superseded_count": stage.superseded_count,
        "failed_calls": stage.failed_calls,
    }


def _resolution_json(resolution: Resolution | None) -> dict[str, object]:
    if resolution is None:
        return {
            "outcome": None,
            "expression": None,
            "decided_by": None,
            "decided_at": None,
            "rationale": None,
        }
    return {
        "outcome": resolution.outcome,
        "expression": resolution.expression,
        "decided_by": resolution.decided_by,
        "decided_at": resolution.decided_at,
        "rationale": resolution.rationale,
    }


def flow_document_json(document: FlowDocument) -> dict[str, object]:
    """The wire shape the client-side flow renders from -- RFC-0007 SS08's schema."""
    return {
        "idea_slug": document.idea_slug,
        "title": document.title,
        "opened": document.opened,
        "stages": [_stage_json(stage) for stage in document.stages],
        "resolution": _resolution_json(document.resolution),
    }
