"""One page that answers "where is everything, and what should I do next" (#34).

The state already exists, scattered: drafts and runs in the run store,
investigations in the corpus, publication marked by a run manifest appearing
under ``03-runs/``. This composes those rather than inventing a second state
model.

Two rules it keeps:

* **Deterministic.** Every bucket and every suggested action is derived from
  state by plain code. Nothing here asks a model what to do next — a task list
  that hallucinates is worse than no task list.
* **It says what it cannot see.** Runs executing in another process, and
  anything that exists only in GitHub issues, are invisible from here. The page
  names those gaps rather than presenting a total that quietly excludes them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from alexandria.commission import RunStore
from alexandria.infrastructure.config import Config
from alexandria.infrastructure.research_repo import list_investigations

if TYPE_CHECKING:
    from alexandria.commission_models import Draft, RunRecord
    from alexandria.infrastructure.research_repo import Investigation

# Matches mcp_server's threshold: past this a "running" record is more likely an
# interrupted server than a slow commission.
STALE_RUN_SECONDS = 45 * 60


@dataclass
class Action:
    """Something to do, derived from state rather than suggested by a model."""

    label: str
    href: str
    why: str


@dataclass
class Survey:
    awaiting_approval: list[Draft] = field(default_factory=list)
    running: list[RunRecord] = field(default_factory=list)
    awaiting_promotion: list[RunRecord] = field(default_factory=list)
    published: list[RunRecord] = field(default_factory=list)
    needs_attention: list[tuple[RunRecord, str]] = field(default_factory=list)
    investigations: list[Investigation] = field(default_factory=list)
    blind_spots: list[str] = field(default_factory=list)

    @property
    def actions(self) -> list[Action]:
        """State to action, one mapping per rule, most urgent first."""
        out: list[Action] = []
        for run, reason in self.needs_attention:
            out.append(
                Action(
                    label=f"Look at run {run.run_id}",
                    href=f"/runs/{run.run_id}",
                    why=reason,
                )
            )
        for draft in self.awaiting_approval:
            out.append(
                Action(
                    label=f"Approve or discard draft {draft.draft_id}",
                    href="/commission",
                    why="drafted but never dispatched; it spends nothing until you say so",
                )
            )
        for run in self.awaiting_promotion:
            out.append(
                Action(
                    label=f"Promote run {run.run_id} into the corpus",
                    href=f"/runs/{run.run_id}",
                    why="finished here, but no manifest for it exists in research/",
                )
            )
        for investigation in self.investigations:
            if "01-brief" in investigation.stages_present and not (
                {"05-analysis", "06-synthesis"} & set(investigation.stages_present)
            ):
                out.append(
                    Action(
                        label=f"Carry {investigation.slug} past its brief",
                        href=f"/flow/{investigation.slug}",
                        why="has a brief and no analysis or synthesis",
                    )
                )
        return out


def _published_run_ids(config: Config) -> set[str]:
    """Runs the corpus has a manifest for.

    publish writes ``03-runs/<run_id>.json``, so the corpus itself records which
    runs were promoted — no second ledger, and nothing to drift.
    """
    research = config.repo_root / "research"
    if not research.is_dir():
        return set()
    return {path.stem for path in research.glob("*/03-runs/*.json") if path.stem != "manifest"}


def survey(config: Config, *, now: datetime | None = None) -> Survey:
    """Compose what is known, and be explicit about what is not."""
    now = now or datetime.now(UTC)
    store = RunStore(config.data_dir)
    result = Survey()

    try:
        runs = store.list_runs()
    except (OSError, ValueError):
        runs = []
    published_ids = _published_run_ids(config)

    for run in runs:
        if run.status == "running":
            elapsed = (now - run.created_at).total_seconds()
            if elapsed > STALE_RUN_SECONDS:
                result.needs_attention.append(
                    (
                        run,
                        (
                            f"marked running for {elapsed / 3600:.1f}h — longer than a "
                            "commission takes, so the server may have been restarted mid-run"
                        ),
                    )
                )
            else:
                result.running.append(run)
        elif run.status == "failed":
            result.needs_attention.append((run, "the run failed; no models produced usable output"))
        elif run.status == "partial":
            result.needs_attention.append(
                (run, "partial — some models failed, so totals are not a full panel")
            )
        elif run.run_id in published_ids:
            result.published.append(run)
        else:
            result.awaiting_promotion.append(run)

    result.published.extend(
        run for run in runs if run.status in {"failed", "partial"} and run.run_id in published_ids
    )

    drafts_dir = Path(store.drafts_dir)
    if drafts_dir.is_dir():
        for path in sorted(drafts_dir.glob("*.json")):
            try:
                draft = store.load_draft(path.stem)
            except (OSError, ValueError):
                continue
            # A draft whose run already exists is history, not a decision.
            # Same hash dispatch records, so "already dispatched" is a fact
            # rather than a guess about ids.
            drafted = hashlib.sha256(draft.brief.verbatim().encode()).hexdigest()
            if not any(run.brief_sha256 == drafted for run in runs):
                result.awaiting_approval.append(draft)

    try:
        result.investigations = list_investigations(config)
    except (OSError, ValueError):
        result.investigations = []

    result.blind_spots = [
        (
            "Runs dispatched by another process (the MCP server and the web surface "
            "each see only their own in-flight work)."
        ),
        (
            "Anything proposed only in GitHub issues — this page reads local state "
            "and the corpus, and never the network."
        ),
        (
            "Whether a published investigation reached the website; publish writes "
            "into research/ and stops there."
        ),
    ]
    return result
