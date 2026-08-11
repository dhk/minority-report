"""Promote a completed run into the research corpus.

A commission finishes in the run store and stops there. Nothing moves it into
``research/``, so commissioned work can never become published work. This is
that step (#40).

Three rules shape everything here:

* **It drafts; it never commits.** ``AGENTS.md`` rule 7: saving and committing
  the corpus is the operator's deliberate act. This writes into the working
  tree and stops, so the diff can be read before anything becomes the record.
* **Raw provider responses stay local.** ``dhk/alexandria`` is public. The
  manifest records that raw responses exist, with their hashes and where they
  live, so the record is complete without disclosing. Extracted quotes in
  ``scores.csv`` do publish -- the operator chooses what a brief contains.
* **Every dispatched model appears.** Rule 5: a failed or silent model is a
  recorded observation, never an absence.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from alexandria.commission import RunStore
from alexandria.commission_models import RunRecord
from alexandria.infrastructure.config import Config

# The report describes one run, so it lands in analysis. 06-synthesis is where
# a human draws across runs, and a tool inventing a synthesis from a single
# commission would be claiming more than it has.
REPORT_STAGE = "05-analysis"

_EDITORIAL_PLACEHOLDER = "TODO — the operator writes this; a tool cannot."


class PublishError(RuntimeError):
    """The run cannot be promoted without losing or overwriting something."""


@dataclass
class PublishResult:
    investigation: Path
    written: list[Path] = field(default_factory=list)
    needs_operator: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Drafted into {self.investigation}", ""]
        lines += [f"  wrote {path}" for path in self.written]
        if self.needs_operator:
            lines += ["", "Still needs you:"]
            lines += [f"  - {item}" for item in self.needs_operator]
        lines += [
            "",
            "Nothing was committed. Read the diff, then commit it yourself —",
            "the corpus is the record because a human puts it there.",
        ]
        return "\n".join(lines)


def _topic_yaml(run: RunRecord, slug: str, title: str) -> str:
    """The machine-known fields, and honest gaps for the rest.

    A real topic.yaml carries framing — why now, what is under test, what is out
    of scope — that no tool can derive from a run. Writing plausible-looking
    values for those would put invented editorial judgement into the
    authoritative record, so they are written as visible TODOs instead.
    """
    models = "\n".join(f"  - {model}" for model in run.dispatched_models)
    return f"""# Alexandria investigation metadata.
# Drafted by `publish` from run {run.run_id}. The fields below the marker are
# editorial and were left for the operator: a tool cannot know why this
# question was asked or what was deliberately out of scope.

title: "{title}"
status: "commissioned-draft"
assurance_level: "bronze"

slug: {slug}
opened: {run.created_at.date().isoformat()}

commissioned_run: {run.run_id}
dispatched_models:
{models}
grading_model: {run.grading_model}

# ---- editorial: nothing below here was written by a tool ----
origin: >
  {_EDITORIAL_PLACEHOLDER}
claim_under_test: >
  {_EDITORIAL_PLACEHOLDER}
why_now: >
  {_EDITORIAL_PLACEHOLDER}
scope: >
  {_EDITORIAL_PLACEHOLDER}
"""


def _run_manifest(run: RunRecord, run_dir: Path) -> str:
    """What happened, including what raw responses exist without carrying them.

    Hashes rather than bodies: the published record can prove which response it
    was graded against, and the body stays on the host.
    """
    raw: list[dict[str, str | int]] = []
    raw_dir = run_dir / "raw"
    if raw_dir.is_dir():
        for path in sorted(raw_dir.glob("*.json")):
            payload = path.read_bytes()
            raw.append(
                {
                    "file": path.name,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                }
            )
    return (
        json.dumps(
            {
                "run_id": run.run_id,
                "status": run.status,
                "created_at": run.created_at.isoformat(),
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "dispatched_models": run.dispatched_models,
                "grading_model": run.grading_model,
                "cost_actual": run.cost_actual,
                "cost_estimate": run.cost_estimate,
                "web_search": run.web_search,
                "brief_sha256": run.brief_sha256,
                # Not published. Recorded so the omission is visible rather than
                # looking like the responses never existed.
                "raw_responses": {
                    "published": False,
                    "reason": "raw provider responses stay on the host; the corpus is public",
                    "files": raw,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _quotes_kept(scores_csv: str) -> str:
    """scores.csv passes through intact, quotes included.

    Kept as an explicit step rather than a plain copy so that the decision --
    extracted quotes publish, raw bodies do not -- is visible in the code that
    implements it.
    """
    reader = csv.DictReader(io.StringIO(scores_csv))
    rows = list(reader)
    if not rows:
        return scores_csv
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames or list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def publish_run(
    config: Config,
    run_id: str,
    slug: str,
    *,
    title: str = "",
    overwrite: bool = False,
) -> PublishResult:
    """Draft a completed run into ``research/<slug>/`` and stop."""
    store = RunStore(config.data_dir)
    run = store.load_run(run_id.strip())
    if run.status not in ("completed", "partial"):
        raise PublishError(
            f"Run {run.run_id} is {run.status}. Only a finished run can be published; "
            "a partial one publishes with its failures recorded, a running one cannot."
        )

    run_dir = store.run_dir(run.run_id)
    investigation = config.repo_root / "research" / slug.strip()
    result = PublishResult(investigation=investigation)

    sources: list[tuple[str, str]] = []
    brief = run_dir / "brief.md"
    if brief.is_file():
        sources.append(("01-brief/brief.md", brief.read_text(encoding="utf-8")))
    report = run_dir / "report.md"
    if report.is_file():
        sources.append((f"{REPORT_STAGE}/analysis.md", report.read_text(encoding="utf-8")))
    claims = run_dir / "claims.json"
    if claims.is_file():
        sources.append((f"{REPORT_STAGE}/claims.json", claims.read_text(encoding="utf-8")))
    scores = run_dir / "scores.csv"
    if scores.is_file():
        sources.append((f"{REPORT_STAGE}/scores.csv", _quotes_kept(scores.read_text("utf-8"))))
    sources.append((f"03-runs/{run.run_id}.json", _run_manifest(run, run_dir)))

    topic = investigation / "topic.yaml"
    if not topic.exists():
        sources.append(("topic.yaml", _topic_yaml(run, slug.strip(), title or slug.strip())))
        result.needs_operator.append(
            "topic.yaml carries TODO placeholders for origin, claim_under_test, "
            "why_now and scope — a tool cannot write those"
        )
        if not title:
            result.needs_operator.append(
                f'title is the slug ("{slug.strip()}") because none was given'
            )

    clashes = [rel for rel, _ in sources if (investigation / rel).exists()]
    if clashes and not overwrite:
        raise PublishError(
            "Refusing to overwrite existing corpus files: "
            + ", ".join(sorted(clashes))
            + ". Pass overwrite=True only if replacing them is what you mean."
        )

    for relative, body in sources:
        target = investigation / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        result.written.append(target)

    if run.status == "partial":
        result.needs_operator.append(
            "this run was partial — check the manifest for which models failed "
            "before presenting it as a three-model result"
        )
    return result
