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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from alexandria.commission import (
    DEFAULT_GRADING_MODEL,
    DEFAULT_MODELS,
    CommissionError,
    CommissionService,
    OpenRouterGateway,
    RunStore,
)
from alexandria.commission_models import Brief, Draft
from alexandria.infrastructure.config import Config, RepoNotFoundError, load_config
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
from alexandria.version import service_version

server = FastMCP("alexandria")

_STARTED_AT = datetime.now(UTC).isoformat()


def _config_or_message() -> Config | str:
    try:
        return load_config()
    except RepoNotFoundError as exc:
        return str(exc)


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


def _draft_review(draft: Draft) -> str:
    confirmation = f"RUN {draft.draft_id}"
    if draft.estimate_usd is None:
        estimate = "unavailable"
        pricing_note = (
            "The run may still be dispatched; the OpenRouter key limit is the active ceiling."
        )
    else:
        estimate = f"${draft.estimate_usd:.4f} maximum"
        pricing_note = (
            "Dispatch is blocked because the estimate exceeds the hard ceiling."
            if draft.estimate_usd > draft.ceiling_usd
            else "The estimate is within the hard ceiling."
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
        "Web search: on — research models read live sources, so this run is not reproducible."
        if draft.web_search
        else "Web search: off — research models answer from training data only."
    )
    return "\n".join(
        [
            "Research draft ready — no provider model calls have been dispatched.",
            f"Draft: {draft.draft_id}",
            f"Estimate: {estimate}",
            f"Hard ceiling: ${draft.ceiling_usd:.2f}",
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
    web_search: bool = True,
) -> str:
    """Begin a research commission from pasted text and/or a supported GitHub URL.

    The URL may identify a GitHub repository, issue, pull request, or supported
    text/PDF/HTML/Markdown blob. This tool resolves inputs, performs a live pricing
    lookup, and saves a local review draft. It does NOT call research or grading
    models. Show the returned review to the operator; do not call ``run_research``
    unless the operator explicitly approves it.

    ``web_search`` gives the research models live web access through OpenRouter's
    web plugin, so claims can rest on fetched sources rather than recalled ones.
    It is on by default. Turning it off makes a run reproducible from its inputs
    alone, at the cost of everything the models cannot already recall.
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
    try:
        async with OpenRouterGateway(openrouter_api_key()) as gateway:
            run = await CommissionService(config, gateway).dispatch(draft.draft_id)
    except (CommissionError, OSError, SecretNotFoundError, ValueError) as exc:
        return f"Run did not start: {exc}"
    actual_cost = f"${run.cost_actual:.4f}" if run.cost_actual is not None else "unavailable"
    return "\n".join(
        [
            "Research run finished.",
            f"Run: {run.run_id}",
            f"Status: {run.status}",
            f"Actual cost: {actual_cost}",
            f"Artifacts: {RunStore(config.data_dir).run_dir(run.run_id)}",
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
    except RepoNotFoundError as exc:
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
    family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((args.host, args.port))
        except OSError as exc:
            print(
                f"ERROR: cannot bind {args.host}:{args.port} ({exc.strerror}). "
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
