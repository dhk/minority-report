"""Alexandria MCP server: repository recall plus guarded research commissions.

The recall tools are read-only and deterministic. ``begin_research`` resolves
operator-supplied inputs and creates a local review draft; ``run_research`` is
the separate, explicitly confirmed OpenRouter spend boundary.
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import secrets
import socket
import subprocess
import sys
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from alexandria.background import start as start_dispatch
from alexandria.commission import (
    DEFAULT_GRADING_MODEL,
    DEFAULT_MODELS,
    MAX_COMPLETION_TOKENS,
    CommissionError,
    CommissionService,
    OpenRouterGateway,
    RunStore,
)
from alexandria.commission_models import Brief, Draft
from alexandria.infrastructure.config import (
    Config,
    HostEnvironmentError,
    RepoNotFoundError,
    load_config,
)
from alexandria.infrastructure.mcp_process import clear_pidfile, read_server_pid, write_pidfile
from alexandria.infrastructure.research_repo import (
    LIFECYCLE_STAGES,
    find_investigation,
    list_investigations,
    search_investigations,
)
from alexandria.infrastructure.secrets import SecretNotFoundError, openrouter_api_key
from alexandria.input_resolution import (
    GitHubResolver,
    InputResolutionError,
    pasted_input,
    validate_input_set,
)
from alexandria.publish import PublishError
from alexandria.publish import publish_run as publish_run_to_corpus
from alexandria.resolution import RESOLUTION_FILENAME, ResolutionError
from alexandria.resolution import draft_resolution as draft_resolution_model
from alexandria.version import service_version

server = FastMCP("alexandria")

_STARTED_AT = datetime.now(UTC).isoformat()


def _config_or_message() -> Config | str:
    try:
        return load_config()
    except (HostEnvironmentError, RepoNotFoundError) as exc:
        return str(exc)


# Longer than any plausible commission. Past this, a record still marked
# running is more likely an interrupted server than a slow one (#33).
_STALE_RUN_SECONDS = 45 * 60


@server.tool()
def status() -> str:
    """Alexandria repository status: investigation counts by lifecycle
    stage and assurance level. Read-only, no model call.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    investigations = list_investigations(config)
    if not investigations:
        return (
            f"Alexandria repo: {config.repo_root}\n"
            "No research investigations yet — nothing under research/."
        )
    by_stage = Counter(inv.current_stage or "no lifecycle dirs yet" for inv in investigations)
    by_assurance = Counter(inv.assurance_level or "unset" for inv in investigations)
    lines = [
        f"Alexandria repo: {config.repo_root}",
        f"Investigations: {len(investigations)}",
        "By stage:",
        *(f"  {stage}: {count}" for stage, count in sorted(by_stage.items())),
        "By assurance level:",
        *(f"  {level}: {count}" for level, count in sorted(by_assurance.items())),
    ]
    return "\n".join(lines)


@server.tool()
def list_research(assurance: str = "", stage: str = "") -> str:
    """List research investigations under research/, newest evidence first
    is NOT implied — sorted by slug. Optionally filter by assurance level
    ('bronze'/'silver'/'gold') or current lifecycle stage (e.g. '03-runs').
    Read-only against the git repo as system of record — no model call,
    no network.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    investigations = list_investigations(config)
    if assurance.strip():
        wanted = assurance.strip().lower()
        investigations = [inv for inv in investigations if inv.assurance_level == wanted]
    if stage.strip():
        wanted_stage = stage.strip()
        investigations = [inv for inv in investigations if inv.current_stage == wanted_stage]
    if not investigations:
        return "No research investigations match."
    lines = []
    for inv in investigations:
        title = inv.title or "(untitled)"
        assurance_label = inv.assurance_level or "unset"
        stage_label = inv.current_stage or "no lifecycle dirs yet"
        lines.append(f"{inv.slug} — {title} [{assurance_label}, stage={stage_label}]")
    return "\n".join(lines)


@server.tool()
def show_research(slug: str) -> str:
    """Full detail for one research investigation: topic.yaml's title/status/
    assurance level, its README, and which of the nine lifecycle stages
    (00-topic..08-published) exist so far. `slug` is the directory name
    under research/ (see list_research).
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    investigation = find_investigation(config, slug.strip())
    if investigation is None:
        return f"No investigation named {slug!r} under research/. See list_research."
    lines = [
        f"{investigation.slug} — {investigation.title or '(untitled)'}",
        f"Path: {investigation.path}",
        f"Assurance level: {investigation.assurance_level or 'unset'}",
        f"Status: {investigation.status or 'unset'}",
        "Lifecycle stages present:",
    ]
    lines.extend(
        f"  [{'x' if stage in investigation.stages_present else ' '}] {stage}"
        for stage in LIFECYCLE_STAGES
    )
    readme = investigation.path / "README.md"
    if readme.is_file():
        lines.append("")
        lines.append("README.md:")
        lines.append(readme.read_text(encoding="utf-8"))
    return "\n".join(lines)


@server.tool()
def search_research(query: str, limit: int = 10) -> str:
    """Case-insensitive substring search across every research/ text file
    (.md/.yaml/.yml/.txt/.json), returning file/line citations. A starting
    point for "what does the repo already say about X" — not a semantic
    search, and not a replacement for reading the matched artifact.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    limit = max(1, min(limit, 50))
    hits = search_investigations(config, query, limit=limit)
    if not hits:
        return f"No matches for {query!r} under research/."
    lines = [f"{hit.slug}/{hit.relative_path}:{hit.line_number}: {hit.snippet}" for hit in hits]
    return "\n".join(lines)


@server.tool()
def draft_resolution(
    slug: str,
    outcome: str,
    expression: str = "",
    decided_by: str = "",
    decided_at: str = "",
    rationale: str = "",
) -> str:
    """Validate an idea's resolution (issue #35's taxonomy) and return the
    resolution.yaml text to save, without writing anything.

    `outcome` must be one of 'implemented', 'morphed', or 'nixed' -- there is
    no fourth "back-burnered" value; an idea with no resolution.yaml is simply
    unresolved. 'morphed' requires `expression` (a forward pointer to what the
    idea became) -- morphed without one is itself a dead end and is rejected.

    Like every other write-shaped tool here, this does NOT touch the git
    working tree. research/ only changes by a deliberate operator commit
    (DESIGN.md) -- this tool validates and drafts the file; saving it to
    research/<slug>/resolution.yaml and committing it is the operator's own
    action.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    investigation = find_investigation(config, slug.strip())
    if investigation is None:
        return f"No investigation named {slug!r} under research/. See list_research."
    try:
        resolution = draft_resolution_model(
            outcome=outcome,
            expression=expression,
            decided_by=decided_by,
            decided_at=decided_at,
            rationale=rationale,
        )
    except ResolutionError as exc:
        return f"Resolution not valid: {exc}"
    save_path = investigation.path / RESOLUTION_FILENAME
    return "\n".join(
        [
            f"Resolution for {investigation.slug} validates.",
            f"Save this to {save_path} and commit it -- this tool has not written anything:",
            "",
            resolution.to_yaml().rstrip(),
        ]
    )


def _draft_review(draft: Draft) -> str:
    confirmation = f"RUN {draft.draft_id}"
    if draft.estimate_usd is None:
        estimate = "unavailable"
        pricing_note = (
            "The run may still be dispatched; the OpenRouter key limit is the active ceiling."
        )
    else:
        # Not "maximum". Runs have come in at ~2x this figure, so calling it a
        # ceiling made the one step that costs real money misleading
        # (dhk/alexandria#32). The hard ceiling below is the only real bound.
        estimate = f"${draft.estimate_usd:.4f}"
        pricing_note = (
            "Dispatch is blocked because the estimate exceeds the hard ceiling."
            if draft.estimate_usd > draft.ceiling_usd
            else "The estimate is within the hard ceiling."
        )
    estimate_lines: list[str] = []
    detail = draft.estimate_detail
    if detail is not None:
        models = detail.research_model_count
        rows = [("research", f"{models} models", detail.research_usd)]
        if detail.web_search_usd:
            rows.append(("web search", f"{models} searches", detail.web_search_usd))
        rows.append(("grading", "1 model", detail.grading_usd))
        estimate_lines = [
            f"  {label:<11}{basis:<12}{f'${amount:.4f}':>8}" for label, basis, amount in rows
        ]
        estimate_lines.append(
            f"  assumes {detail.assumed_completion_tokens:,} completion tokens per model."
        )
        estimate_lines.append(
            "  Not covered: longer answers, retries, and calls that fail after billing."
        )
    input_lines = [
        (
            f"  - {item.name}: {item.state}, {item.bytes} bytes, "
            f"{item.extracted_chars} chars, sha256={item.sha256[:12]}"
        )
        for item in draft.inputs
    ]
    model_lines = [f"  - {model}" for model in draft.models]
    web_search_line = (
        "Web search: on — research models read live sources, so this run is not reproducible.\n"
        f"  Reason given: {draft.web_search_rationale}\n"
        "  Search results are billed as prompt tokens. On the one brief measured both\n"
        "  ways this took the run from $0.75 to $3.03."
        if draft.web_search
        else "Web search: off — research models answer from training data only."
    )
    detail = draft.estimate_detail
    ceiling_line = (
        f"Hard ceiling: ${draft.ceiling_usd:.2f}. This run cannot exceed "
        f"${detail.worst_case_usd:.4f} — every model writing to the "
        f"{MAX_COMPLETION_TOKENS:,}-token cap — and dispatch refuses if that "
        "worst case does not fit."
        if detail is not None and detail.worst_case_usd is not None
        else f"Hard ceiling: ${draft.ceiling_usd:.2f}, checked against the estimate only: "
        "live pricing was unavailable, so no bound could be computed."
    )
    return "\n".join(
        [
            "Research draft ready — no provider model calls have been dispatched.",
            f"Draft: {draft.draft_id}",
            f"Estimate: {estimate}",
            *estimate_lines,
            ceiling_line,
            pricing_note,
            "",
            "Inputs:",
            *input_lines,
            "",
            "Research models:",
            *model_lines,
            f"Grading model: {draft.grading_model}",
            web_search_line,
            "",
            "Brief sent verbatim:",
            draft.brief.verbatim(),
            "",
            "Dispatch requires the operator to explicitly approve this review.",
            (
                f'After approval, call run_research(draft_id="{draft.draft_id}", '
                f'confirmation="{confirmation}").'
            ),
        ]
    )


@server.tool()
def list_runs(limit: int = 20) -> str:
    """Commissioned runs on this host, newest first: id, status, cost, models.

    Reads the local run store, not the committed corpus. ``status`` and
    ``list_research`` read ``research/`` and will not show a run until someone
    deliberately promotes it, so they are not a signal about a run that just
    finished — or one still going (#33).
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    try:
        runs = RunStore(config.data_dir).list_runs()
    except (CommissionError, OSError, ValueError) as exc:
        return f"Runs unavailable: {exc}"
    if not runs:
        return "No commissioned runs on this host."
    lines = [f"{len(runs)} run(s), newest first:"]
    for run in runs[: max(1, limit)]:
        cost = f"${run.cost_actual:.4f}" if run.cost_actual is not None else "—"
        lines.append(
            f"  {run.run_id} · {run.status} · {cost} · "
            f"{len(run.dispatched_models)} model(s) · started {run.created_at.isoformat()}"
        )
    return "\n".join(lines)


@server.tool()
def run_status(run_id: str) -> str:
    """What happened to one commissioned run, by id.

    Exists so a caller who lost ``run_research``'s reply to a client-side
    timeout can still find out whether the run happened, what it cost, and
    which models answered (#33). The work continues on the server regardless of
    whether anyone is still listening for the reply.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    try:
        run = RunStore(config.data_dir).load_run(run_id.strip())
    except (CommissionError, OSError, ValueError) as exc:
        return f"Run unavailable: {exc}"
    lines = [
        f"Run: {run.run_id}",
        f"Status: {run.status}",
        f"Started: {run.created_at.isoformat()}",
    ]
    if run.completed_at is not None:
        lines.append(f"Completed: {run.completed_at.isoformat()}")
    if run.status == "running":
        elapsed = (datetime.now(UTC) - run.created_at).total_seconds()
        lines.append(f"Elapsed: {elapsed / 60:.1f} min")
        if elapsed > _STALE_RUN_SECONDS:
            # A record left "running" long past any plausible commission is
            # more likely an interrupted server than a slow one. Saying so
            # beats reporting progress that is not happening.
            lines.append(
                "  WARNING: this has been running far longer than a commission takes. "
                "The server may have been restarted mid-run; this record will not "
                "update itself."
            )
    cost = f"${run.cost_actual:.4f}" if run.cost_actual is not None else "unavailable"
    lines.append(
        f"Cost: {cost} (estimate ${run.cost_estimate:.4f})"
        if run.cost_estimate
        else f"Cost: {cost}"
    )
    lines.append(f"Models dispatched: {', '.join(run.dispatched_models) or 'none'}")
    return "\n".join(lines)


@server.tool()
def publish_run(run_id: str, slug: str, title: str = "", overwrite: bool = False) -> str:
    """Draft a finished run into the research corpus as investigation ``slug``.

    Writes into the corpus working tree and stops. It does not commit and does
    not push: the corpus is authoritative because a human puts things there
    (AGENTS.md rule 7). Read the diff, then commit it yourself.

    Raw provider responses are not published -- the corpus is public. The
    manifest records that they exist, with hashes, so the omission is visible.
    Extracted quotes in scores.csv do publish.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    try:
        result = publish_run_to_corpus(config, run_id, slug, title=title, overwrite=overwrite)
    except (CommissionError, PublishError, OSError, ValueError) as exc:
        return f"Nothing was published: {exc}"
    return result.summary()


@server.tool()
async def begin_research(
    task: str,
    pasted_content: str = "",
    url: str = "",
    context: str = "",
    constraints: str = "",
    output_needs: str = "",
    models: list[str] | None = None,
    grading_model: str = DEFAULT_GRADING_MODEL,
    ceiling_usd: float = 1.0,
    web_search: bool = False,
    web_search_rationale: str = "",
) -> str:
    """Begin a research commission from pasted text and/or a supported GitHub URL.

    The URL may identify a GitHub repository, issue, pull request, or supported
    text/PDF/HTML/Markdown blob. This tool resolves inputs, performs a live pricing
    lookup, and saves a local review draft. It does NOT call research or grading
    models. Show the returned review to the operator; do not call ``run_research``
    unless the operator explicitly approves it.

    ``web_search`` gives the research models live web access through OpenRouter's
    web plugin, so claims can rest on fetched sources rather than recalled ones.
    It is OFF by default, and turning it on requires ``web_search_rationale``.

    Do not pass ``web_search=True`` because the brief sounds researchy. Ask the
    operator what it is for, and pass their answer. Search earns its cost when
    the question turns on something training data cannot settle: events after
    the model's cutoff, a specific document that must be read as it stands
    today, a contested claim where the current state of the argument matters.
    It does not earn its cost for established standards, textbook material, or
    anything the models already know — and on the one brief measured both ways
    it quadrupled the bill, $0.75 to $3.03, because search results are billed
    as prompt tokens on top of everything else. It also costs the run its
    reproducibility: a searching run rests on pages as they read that day.

    If the operator's reason amounts to "it might find something useful", that
    is a no. Say so, and offer to run it without search first — the offline run
    is cheap enough to be the default attempt, and its gaps tell you whether
    search was needed after all.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    if not math.isfinite(ceiling_usd) or ceiling_usd <= 0:
        return "Commission not ready: ceiling_usd must be a positive finite amount."
    if not grading_model.strip():
        return "Commission not ready: grading_model is required."
    try:
        inputs = []
        pasted = pasted_input(pasted_content)
        if pasted is not None:
            inputs.append(pasted)
        resolved_url = url.strip()
        if resolved_url:
            async with GitHubResolver() as resolver:
                inputs.extend(await resolver.resolve(resolved_url))
        validate_input_set(inputs)
        selected_models = list(DEFAULT_MODELS if models is None else models)
        async with OpenRouterGateway(openrouter_api_key()) as gateway:
            draft = await CommissionService(config, gateway).create_draft(
                Brief(
                    task=task,
                    context=context,
                    constraints=constraints,
                    output_needs=output_needs,
                ),
                inputs,
                selected_models,
                grading_model.strip(),
                ceiling_usd,
                web_search,
                web_search_rationale,
            )
        return _draft_review(draft)
    except (
        CommissionError,
        httpx.HTTPError,
        InputResolutionError,
        OSError,
        SecretNotFoundError,
        ValueError,
    ) as exc:
        return f"Commission not ready: {exc}"


@server.tool()
async def run_research(draft_id: str, confirmation: str = "") -> str:
    """Dispatch a reviewed research draft, incurring OpenRouter spend.

    First call ``begin_research`` and show its complete review to the operator.
    Call this tool only after the operator explicitly approves that review and
    supplies the exact confirmation phrase returned with it. A missing or stale
    phrase leaves the draft untouched and dispatches nothing.

    **This returns as soon as the run has an id — it does not wait for the
    commission to finish** (#33). The research models, web search and grading
    continue in the background and write their results to the run store, which
    takes several minutes. The reply tells you the run id; ``run_status`` tells
    you how it ended. Do not dispatch the same draft again while one is running
    -- that spends the budget twice.
    """
    config = _config_or_message()
    if isinstance(config, str):
        return config
    try:
        draft = RunStore(config.data_dir).load_draft(draft_id.strip())
    except (CommissionError, OSError, ValueError) as exc:
        return f"Run did not start: {exc}"
    expected = f"RUN {draft.draft_id}"
    if confirmation != expected:
        return "\n".join(
            [
                _draft_review(draft),
                "",
                "Dispatch blocked: explicit operator approval is required.",
                f'Use the exact confirmation phrase: "{expected}"',
            ]
        )

    @asynccontextmanager
    async def _gateway() -> AsyncIterator[Any]:
        # Built here, from this module's names, so the gateway stays patchable
        # where it always was. background.start only runs it.
        async with OpenRouterGateway(openrouter_api_key()) as gateway:
            yield gateway

    try:
        run = await start_dispatch(config, draft.draft_id, gateway_cm=_gateway)
    except (CommissionError, OSError, SecretNotFoundError, ValueError) as exc:
        # The run record is written before any model is called, so a failure
        # here still leaves something findable. Say so rather than implying
        # nothing happened.
        return (
            f"Run did not finish: {exc}\n"
            "If a run id was allocated, `list_runs` will show it and `run_status` "
            "will say how far it got."
        )
    return "\n".join(
        [
            "Research run started. It is running now; this reply did not wait for it.",
            f"Run: {run.run_id}",
            f"Models: {', '.join(run.dispatched_models)}",
            f"Estimate: ${run.cost_estimate:.4f}" if run.cost_estimate else "Estimate: unavailable",
            f"Artifacts: {RunStore(config.data_dir).run_dir(run.run_id)}",
            "",
            (
                f'Check on it with run_status("{run.run_id}"). '
                "Do not dispatch again — that would spend the budget twice."
            ),
        ]
    )


async def _health(request: Request) -> JSONResponse:
    """Unauthenticated, minimal JSON health check — what the admin
    installations page polls. Never put repository data in this.
    """
    return JSONResponse(
        {"service": "alexandria", "version": service_version(), "started_at": _STARTED_AT}
    )


_health_registered = False


def register_health(mcp_server: FastMCP) -> None:
    global _health_registered
    if _health_registered:
        return
    _health_registered = True
    mcp_server.custom_route("/health", methods=["GET"])(_health)


_TOKEN_FILENAME = "mcp-http-token"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _http_token(config: Config, rotate: bool = False) -> str:
    """The capability-path token for the HTTP transport. Generated once
    into the local state dir (never the research repo) with owner-only
    permissions; rotating it is how a leaked URL is revoked.
    """
    path = config.data_dir / _TOKEN_FILENAME
    if rotate or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


def _tailscale_dns_name(runner: Callable[..., Any] = subprocess.run) -> str | None:
    """Return this machine's Tailscale DNS name when it can be detected.

    Detection is deliberately best-effort: Alexandria must still start on a
    machine without Tailscale or while the daemon is unavailable.
    """
    try:
        process = runner(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        name = json.loads(process.stdout)["Self"]["DNSName"]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return str(name).rstrip(".") or None


def _extra_allowed_hosts(
    cli_hosts: Sequence[str] | None,
    env: Mapping[str, str] | None = None,
    tailscale: Callable[[], str | None] = _tailscale_dns_name,
) -> list[str]:
    env = os.environ if env is None else env
    merged = list(cli_hosts or [])
    merged.extend(env.get("ALEXANDRIA_ALLOWED_HOSTS", "").split(","))
    detected = tailscale()
    if detected:
        merged.append(detected)
    ordered: dict[str, None] = {}
    for entry in merged:
        host = entry.strip().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        if host:
            ordered.setdefault(host, None)
    return list(ordered)


def _tunnel_path(cli_value: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """External path used by a stripping tunnel such as Tailscale --set-path."""
    env = os.environ if env is None else env
    raw = cli_value if cli_value is not None else env.get("ALEXANDRIA_TUNNEL_PATH", "")
    value = raw.strip().strip("/")
    return f"/{value}" if value else ""


def _tunnel_port(cli_value: int | None = None, env: Mapping[str, str] | None = None) -> int | None:
    """External HTTPS port, independent from Alexandria's local bind port."""
    if cli_value is not None:
        return cli_value
    env = os.environ if env is None else env
    raw = env.get("ALEXANDRIA_TUNNEL_PORT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def connector_urls(
    token: str,
    extra_hosts: Sequence[str],
    host: str = "127.0.0.1",
    port: int = 8797,
    tunnel_path: str = "",
    tunnel_port: int | None = None,
) -> list[tuple[str, str]]:
    pairs = [("MCP over HTTP", f"http://{host}:{port}/mcp/{token}")]
    path = _tunnel_path(tunnel_path)
    for tunnel_host in extra_hosts:
        authority = tunnel_host if tunnel_port is None else f"{tunnel_host}:{tunnel_port}"
        pairs.append(("Tunnel MCP connector", f"https://{authority}{path}/mcp/{token}"))
    return pairs


def render_urls(
    token: str,
    extra_hosts: Sequence[str],
    host: str = "127.0.0.1",
    port: int = 8797,
    tunnel_path: str = "",
    tunnel_port: int | None = None,
) -> list[str]:
    return [
        f"{label}: {url}"
        for label, url in connector_urls(token, extra_hosts, host, port, tunnel_path, tunnel_port)
    ]


def build_transport_security(extra_hosts: Sequence[str]) -> TransportSecuritySettings:
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for host in extra_hosts:
        allowed_hosts += [host, f"{host}:*"]
        allowed_origins += [
            f"https://{host}",
            f"https://{host}:*",
            f"http://{host}",
            f"http://{host}:*",
        ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _port_available(host: str, port: int) -> str | None:
    """Ask the port question the way the server that follows will ask it.

    Returns None when the port is bindable, otherwise the OS error text.

    ``SO_REUSEADDR`` is not a nicety here. asyncio's ``create_server`` —
    which uvicorn runs underneath — sets it on POSIX, so a socket the
    kernel is holding in TIME_WAIT after a client disconnected is not a
    conflict for the server. A probe without it disagrees with the server
    it is guarding: it reports "address already in use" and exits, for a
    port uvicorn would have bound straight through. Measured on lobster,
    that disagreement lasted 62 seconds after a real client detached —
    against the installer's 15-second health window, which is how it
    failed two installs that were themselves fine.

    A port held by a live listener still fails, which is the case this
    probe exists to catch.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            return exc.strerror or str(exc)
    return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="alexandria-mcp",
        description=(
            "Alexandria MCP server. Default: stdio for a local client "
            "(Claude Desktop / Claude Code). With --http: streamable HTTP on "
            "loopback for remote use through a tunnel you run yourself."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve streamable HTTP instead of stdio, at /mcp/<token> (loopback only "
        "by default; the URL is a capability — treat it like a password).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for --http (default 127.0.0.1; non-loopback prints a warning).",
    )
    parser.add_argument("--port", type=int, default=8797, help="Port for --http (default 8797).")
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="Generate a fresh capability token before serving (revokes every old URL).",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        metavar="HOST",
        help="Extra Host header to accept over --http (repeatable). Merged with "
        "ALEXANDRIA_ALLOWED_HOSTS (comma-separated) and this machine's "
        "auto-detected Tailscale DNS name.",
    )
    parser.add_argument(
        "--tunnel-path",
        default=None,
        help="External path used by a stripping tunnel (for example /alexandria with "
        "tailscale funnel --set-path). Changes printed URLs, not the local route. "
        "Falls back to ALEXANDRIA_TUNNEL_PATH.",
    )
    parser.add_argument(
        "--tunnel-port",
        type=int,
        default=None,
        help="External HTTPS port when it is not 443. Changes printed URLs, not the "
        "local bind. Falls back to ALEXANDRIA_TUNNEL_PORT.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("alexandria-mcp %s starting", service_version())
    if not args.http:
        if args.rotate_token:
            parser.error("--rotate-token only makes sense with --http")
        server.run()
        return
    try:
        config = load_config()
    except (HostEnvironmentError, RepoNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    existing = read_server_pid(config)
    if existing is not None:
        print(
            f"ERROR: the HTTP MCP server is already running (pid {existing}). "
            "Stop it first, or give a second instance its own ALEXANDRIA_DATA_DIR.",
            file=sys.stderr,
        )
        sys.exit(1)
    unavailable = _port_available(args.host, args.port)
    if unavailable is not None:
        print(
            f"ERROR: cannot bind {args.host}:{args.port} ({unavailable}). "
            f"Something else holds the port — find it with: lsof -ti :{args.port}",
            file=sys.stderr,
        )
        sys.exit(1)
    token = _http_token(config, rotate=args.rotate_token)
    server.settings.host = args.host
    server.settings.port = args.port
    server.settings.streamable_http_path = f"/mcp/{token}"
    extra_hosts = _extra_allowed_hosts(args.allowed_host)
    if args.host in _LOOPBACK_HOSTS:
        server.settings.transport_security = build_transport_security(extra_hosts)
    else:
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        print(
            f"WARNING: binding {args.host} exposes this server to that network. "
            "The capability path is the only guard. Prefer 127.0.0.1 plus a tunnel.",
            file=sys.stderr,
        )
    from alexandria.admin import register_admin

    register_health(server)
    register_admin(server)
    for line in render_urls(
        token,
        extra_hosts,
        host=args.host,
        port=args.port,
        tunnel_path=_tunnel_path(args.tunnel_path),
        tunnel_port=_tunnel_port(args.tunnel_port),
    ):
        print(line)
    print(
        "The URL is a capability — anyone holding it can read research/ and, after "
        "draft confirmation, spend through the configured OpenRouter key."
    )
    print("Revoke it any time: alexandria-mcp --http --rotate-token")
    sys.stdout.flush()
    logging.getLogger("uvicorn.access").disabled = True
    write_pidfile(config)
    atexit.register(clear_pidfile, config)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
