"""Read-only access to research/ investigations — the data these MCP tools serve.

No model call, no network, no writes: this module only reads what's
already committed to the repository (docs/DESIGN.md's "the repository is
the durable system of record"). `research/` does not exist yet as of this
writing (README.md's repository map is still aspirational for most of the
tree) — every function here handles that as the ordinary, expected case,
not an error.

`topic.yaml`'s schema is not yet formalized under schemas/ (also not built
yet). Until it is, this reads a lenient, best-effort shape: `title`,
`status`, and `assurance_level` (bronze/silver/gold, case-insensitive) if
present, ignored if absent or malformed. Do not treat this parser as the
schema's source of truth — it follows schemas/ once that exists, not the
other way around.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from alexandria.infrastructure.config import Config

# The nine-stage lifecycle from README.md's repository map, in order.
LIFECYCLE_STAGES = (
    "00-topic",
    "01-brief",
    "02-run-plan",
    "03-runs",
    "04-normalized",
    "05-analysis",
    "06-synthesis",
    "07-review",
    "08-published",
)

_SEARCHABLE_SUFFIXES = {".md", ".yaml", ".yml", ".txt", ".json"}
_SNIPPET_MAX_CHARS = 200


class ResearchRepoError(Exception):
    """A research artifact exists but could not be read (e.g. invalid YAML)."""


@dataclass(frozen=True)
class Investigation:
    slug: str
    path: Path
    title: str | None
    status: str | None
    assurance_level: str | None
    stages_present: tuple[str, ...] = field(default_factory=tuple)
    last_modified: datetime | None = None

    @property
    def current_stage(self) -> str | None:
        return self.stages_present[-1] if self.stages_present else None


@dataclass(frozen=True)
class SearchHit:
    slug: str
    relative_path: str
    """Relative to the investigation directory, NOT research/ — so callers
    can format the full path as f"{slug}/{relative_path}" without double
    counting the slug."""
    line_number: int
    snippet: str


def _parse_topic_yaml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ResearchRepoError(f"{path} is not valid YAML: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _stages_present(investigation_dir: Path) -> tuple[str, ...]:
    return tuple(
        stage
        for stage in LIFECYCLE_STAGES
        if (investigation_dir / stage).is_dir() and any((investigation_dir / stage).iterdir())
    )


def _last_modified(investigation_dir: Path) -> datetime | None:
    mtimes = [entry.stat().st_mtime for entry in investigation_dir.rglob("*") if entry.is_file()]
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), tz=UTC)


def _read_investigation(entry: Path) -> Investigation:
    topic = _parse_topic_yaml(entry / "topic.yaml")
    title = topic.get("title")
    status = topic.get("status")
    assurance = topic.get("assurance_level")
    return Investigation(
        slug=entry.name,
        path=entry,
        title=str(title) if title else None,
        status=str(status) if status else None,
        assurance_level=str(assurance).strip().lower() if assurance else None,
        stages_present=_stages_present(entry),
        last_modified=_last_modified(entry),
    )


def list_investigations(config: Config) -> list[Investigation]:
    """Every investigation directly under research/, sorted by slug.

    Returns [] when research/ does not exist yet — that is the expected
    state of a fresh Alexandria checkout, not an error.
    """
    research_dir = config.research_dir
    if not research_dir.is_dir():
        return []
    return [
        _read_investigation(entry) for entry in sorted(research_dir.iterdir()) if entry.is_dir()
    ]


def find_investigation(config: Config, slug: str) -> Investigation | None:
    for investigation in list_investigations(config):
        if investigation.slug == slug:
            return investigation
    return None


def _candidate_files(research_dir: Path) -> Iterator[Path]:
    for path in sorted(research_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in _SEARCHABLE_SUFFIXES:
            yield path


def search_investigations(config: Config, query: str, limit: int = 10) -> list[SearchHit]:
    """Case-insensitive substring search across research/ text files.

    Deterministic, no ranking beyond file/line order — a starting point
    for "what does the repo already say about X," not a replacement for
    reading the matched artifact.
    """
    research_dir = config.research_dir
    needle = query.strip().lower()
    if not research_dir.is_dir() or not needle:
        return []
    hits: list[SearchHit] = []
    for path in _candidate_files(research_dir):
        if len(hits) >= limit:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relative = path.relative_to(research_dir)
        slug, *rest = relative.parts
        # `rest` is empty only for a file sitting directly under research/,
        # outside any investigation directory — not the expected shape, but
        # handled rather than crashing.
        relative_to_investigation = Path(*rest) if rest else Path(slug)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                hits.append(
                    SearchHit(
                        slug=slug,
                        relative_path=str(relative_to_investigation),
                        line_number=line_number,
                        snippet=line.strip()[:_SNIPPET_MAX_CHARS],
                    )
                )
                if len(hits) >= limit:
                    break
    return hits
