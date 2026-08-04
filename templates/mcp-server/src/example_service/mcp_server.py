"""MCP server scaffold: stdio for a local client (Claude Desktop / Claude
Code), or — with --http — a loopback streamable-HTTP server for remote
clients behind a tunnel you run yourself. This is the pattern wingman's
`mcp_server.py` proved out (RFC-008/017 there); fork this file's shape
rather than starting from an empty FastMCP app.

Fork checklist (see templates/mcp-server/README.md for the full version):
1. Rename the `example_service` package and every APP_NAME/ENV_DATA_DIR
   constant in infrastructure/config.py and infrastructure/keys.py.
2. Replace the `status` / `example_tool` tools below with your real ones.
3. Update pyproject.toml's [project.scripts] entry point name.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import secrets
import socket
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from example_service.infrastructure.config import Config, load_config
from example_service.infrastructure.keys import ensure_env
from example_service.infrastructure.mcp_process import (
    clear_pidfile,
    read_server_pid,
    write_pidfile,
)
from example_service.version import service_version

server = FastMCP("example-service")

_STARTED_AT = datetime.now(UTC).isoformat()


@server.tool()
def status() -> str:
    """Service status: version and where its local state lives. Replace/extend
    with real status once this template has real tools with real state.
    """
    config = load_config()
    return (
        f"example-service: {service_version()}\n"
        f"Local state: {config.data_dir} ({config.data_dir_source})"
    )


@server.tool()
def example_tool(message: str) -> str:
    """A placeholder tool — delete once real tools exist. Echoes `message`
    back so a fresh fork has something to call end-to-end on day one.
    """
    return f"example_tool received: {message!r}"


async def _health(request: Request) -> JSONResponse:
    """Plain-text-free JSON health check the admin installations page polls.
    Deliberately unauthenticated (no secret in a health check) and
    deliberately minimal (no workspace data) — same contract wingman's
    web UI relies on for its own admin page.
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
    """The capability-path token for the HTTP transport.

    Generated once into the local state dir with owner-only permissions;
    rotating it is how a leaked URL is revoked. The token is the ONLY
    guard on --http; treat the URL that carries it like a password.
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
    """Front-door hostnames the HTTP transport should accept beyond
    loopback: --allowed-host flags merged with an EXAMPLE_SERVICE_ALLOWED_HOSTS
    env var (comma-separated; systemd-friendly). Rename the env var to match
    APP_NAME when forking.
    """
    env = os.environ if env is None else env
    merged = list(cli_hosts or [])
    merged.extend(env.get("EXAMPLE_SERVICE_ALLOWED_HOSTS", "").split(","))
    ordered: dict[str, None] = {}
    for entry in merged:
        # tolerate a pasted URL: strip scheme, path, trailing slash
        host = entry.strip().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        if host:
            ordered.setdefault(host, None)
    return list(ordered)


def connector_urls(
    token: str,
    extra_hosts: Sequence[str],
    host: str = "127.0.0.1",
    port: int = 8787,
) -> list[tuple[str, str]]:
    """(label, url) pairs: the loopback MCP connector, plus a tunnel pair
    per accepted hostname. Single source of truth behind the --http startup
    banner.
    """
    pairs = [("MCP over HTTP", f"http://{host}:{port}/mcp/{token}")]
    for tunnel_host in extra_hosts:
        pairs.append(("Tunnel MCP connector", f"https://{tunnel_host}/mcp/{token}"))
    return pairs


def render_urls(
    token: str, extra_hosts: Sequence[str], host: str = "127.0.0.1", port: int = 8787
) -> list[str]:
    return [f"{label}: {url}" for label, url in connector_urls(token, extra_hosts, host, port)]


def build_transport_security(extra_hosts: Sequence[str]) -> TransportSecuritySettings:
    """DNS-rebinding settings for a loopback bind: the SDK's loopback
    allow-list plus each extra hostname. Protection stays ON — a tunnel
    widens the list; it never switches the check off.
    """
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
        prog="example-service-mcp",
        description=(
            "example-service MCP server. Default: stdio for a local client "
            "(Claude Desktop / Claude Code). With --http: streamable HTTP on "
            "loopback for remote use through a tunnel you run yourself (e.g. "
            "'tailscale serve')."
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
        help="Extra Host header to accept over --http (repeatable) — the hostname of "
        "whatever fronts the loopback port. Merged with EXAMPLE_SERVICE_ALLOWED_HOSTS "
        "(comma-separated).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("example-service-mcp %s starting", service_version())
    # Hydrate provider keys the tools need, if any: host file, then workspace file.
    ensure_env(data_dir=load_config().data_dir)
    if not args.http:
        if args.rotate_token:
            parser.error("--rotate-token only makes sense with --http")
        server.run()
        return
    config = load_config()
    # Preflight before anything is printed or rotated: a bind error arriving
    # after the full banner reads like a crash.
    existing = read_server_pid(config)
    if existing is not None:
        print(
            f"ERROR: the HTTP MCP server is already running (pid {existing}). "
            "Stop it first, or give a second instance its own data dir.",
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
            f"WARNING: binding {args.host} exposes this service to that network. "
            "The capability path is the only guard. Prefer 127.0.0.1 plus a tunnel.",
            file=sys.stderr,
        )
    from example_service.admin import register_admin

    register_health(server)
    register_admin(server)
    for line in render_urls(token, extra_hosts, host=args.host, port=args.port):
        print(line)
    print("The URL is a capability — anyone holding it can use this service.")
    print("Revoke it any time: example-service-mcp --http --rotate-token")
    sys.stdout.flush()
    # The capability token lives in the URL path; uvicorn's access log would
    # write it on every request, silently defeating rotation-as-revocation.
    logging.getLogger("uvicorn.access").disabled = True
    write_pidfile(config)
    atexit.register(clear_pidfile, config)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
