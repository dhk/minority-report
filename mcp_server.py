"""Alexandria MCP server: read/status tools over the research/ tree, so a
connected model can see what the repository already knows before proposing
new work — the same stdio/--http shape wingman's mcp_server.py proved out,
forked via templates/mcp-server/.

Every tool here is read-only and deterministic: no model call, no network,
no write. That matches the repository's early stage (docs/DESIGN.md: "the
repository is the durable system of record") — there is no orchestration
harness to dispatch here yet (see docs/orchestration-harness.md for that
separate, not-yet-built concern), so this server has nothing to gate
behind a confirmation step.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import secrets
import socket
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from alexandria.infrastructure.config import Config, RepoNotFoundError, load_config
from alexandria.infrastructure.mcp_process import clear_pidfile, read_server_pid, write_pidfile
from alexandria.infrastructure.research_repo import (
    LIFECYCLE_STAGES,
    find_investigation,
    list_investigations,
    search_investigations,
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


async def _health(request: Request) -> JSONResponse:
    """Unauthenticated, minimal JSON health check — what the admin
    installations page polls. Never put repository data in this.
    """
    return JSONResponse({"version": service_version(), "started_at": _STARTED_AT})


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


def _extra_allowed_hosts(
    cli_hosts: Sequence[str] | None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    env = os.environ if env is None else env
    merged = list(cli_hosts or [])
    merged.extend(env.get("ALEXANDRIA_ALLOWED_HOSTS", "").split(","))
    ordered: dict[str, None] = {}
    for entry in merged:
        host = entry.strip().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        if host:
            ordered.setdefault(host, None)
    return list(ordered)


def connector_urls(
    token: str, extra_hosts: Sequence[str], host: str = "127.0.0.1", port: int = 8787
) -> list[tuple[str, str]]:
    pairs = [("MCP over HTTP", f"http://{host}:{port}/mcp/{token}")]
    for tunnel_host in extra_hosts:
        pairs.append(("Tunnel MCP connector", f"https://{tunnel_host}/mcp/{token}"))
    return pairs


def render_urls(
    token: str, extra_hosts: Sequence[str], host: str = "127.0.0.1", port: int = 8787
) -> list[str]:
    return [f"{label}: {url}" for label, url in connector_urls(token, extra_hosts, host, port)]


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
    parser.add_argument("--port", type=int, default=8787, help="Port for --http (default 8787).")
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
        "ALEXANDRIA_ALLOWED_HOSTS (comma-separated).",
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
    for line in render_urls(token, extra_hosts, host=args.host, port=args.port):
        print(line)
    print("The URL is a capability — anyone holding it can read this repository's research/.")
    print("Revoke it any time: alexandria-mcp --http --rotate-token")
    sys.stdout.flush()
    logging.getLogger("uvicorn.access").disabled = True
    write_pidfile(config)
    atexit.register(clear_pidfile, config)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
