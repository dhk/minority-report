"""Admin installations page: a cross-instance overview for however many
isolated instances of this service end up running (one per user/team,
one per environment, ...). Ported from wingman's `admin.py` almost as-is —
see the services-repo issue for the plan to de-duplicate this instead of
copy-pasting it per project.

Deliberately narrow: an explicit, hand-maintained list of instances (no
filesystem discovery, no cross-user read access), gated by its own
credential separate from any single instance's own capability token, and a
launcher rather than a data merge — it links into each instance's own
surface but never renders anything from inside an instance itself.

Deliberately stays read-only + launcher: it should not grow start/stop/
backup for any instance — mutating another account's process requires real
privilege escalation this page doesn't have and shouldn't gain. Any
self-service action belongs on the instance's OWN surface, gated by that
instance's own token.
"""

from __future__ import annotations

import asyncio
import html
import secrets
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from example_service.infrastructure.config import Config, load_config

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_ADMIN_TOKEN_FILENAME = "installations-token"
_HEALTH_TIMEOUT_SECONDS = 2.0

# A minimal, dependency-free stylesheet. Swap in a real design-tokens module
# in the forked project if one exists.
_ADMIN_CSS = """
:root { --border: #d8d8d8; --border-light: #e8e8e8; --text-head: #111; --text-dim: #666;
  --teal: #0a7a6a; --accent-orange: #b5530a; --accent: #2952c8; --border-radius: 8px;
  --font-mono: ui-monospace, monospace; }
body { margin: 0; font-family: system-ui, sans-serif; }
.admin { max-width: 720px; margin: 0 auto; padding: 24px 16px 64px;
  display: flex; flex-direction: column; gap: 16px; }
.admin h1 { font-size: 20px; color: var(--text-head); margin: 0; }
.instances { border: 1px solid var(--border); border-radius: var(--border-radius);
  overflow: hidden; }
.instance-row { display: grid; grid-template-columns: 1fr auto auto auto; gap: 16px;
  align-items: center; padding: 12px 16px; border-top: 1px solid var(--border-light); }
.instance-row:first-child { border-top: 0; }
.instance-row .name { font-weight: 600; color: var(--text-head); }
.instance-row .meta { font-family: var(--font-mono); font-size: 11px; color: var(--text-dim); }
.instance-row .status { font-family: var(--font-mono); font-size: 11px; text-transform: uppercase;
  letter-spacing: .04em; }
.instance-row .status.up { color: var(--teal); }
.instance-row .status.down { color: var(--accent-orange); }
.instance-row a.open { font-family: var(--font-mono); font-size: 11px; text-transform: uppercase;
  letter-spacing: .04em; color: var(--accent); text-decoration: none; }
.instance-row a.open:hover { text-decoration: underline; }
.empty { border: 1px dashed var(--border); padding: 24px 16px; border-radius: var(--border-radius);
  text-align: center; color: var(--text-dim); }
.empty code { font-family: var(--font-mono); }
"""


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def admin_token(config: Config, rotate: bool = False) -> str:
    """The capability token gating the installations page.

    Separate from any single instance's own mcp-http-token: this view spans
    every configured instance, so it must not be reachable with any one of
    their tokens, including the admin's own instance's token.
    """
    path = config.data_dir / _ADMIN_TOKEN_FILENAME
    if rotate or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Instance:
    """'stripped' distinguishes what 'prefix' means: False (default, native
    '--prefix') means the backend itself registers routes under it, same as
    the public URL. True means a fronting proxy strips it before the
    backend ever sees it — the public URL still carries the prefix, but the
    local process listens bare. Health checks need the backend's real local
    path, which only matches 'prefix' when stripped is False.
    """

    name: str
    host: str
    port: int
    prefix: str
    token: str
    tunnel_host: str | None = None
    tunnel_port: int | None = None
    stripped: bool = False


class InstallationsConfigError(Exception):
    """installations.toml exists but could not be parsed."""


def load_instances(config: Config) -> list[Instance]:
    """The hand-maintained instance list, or [] if none is configured yet."""
    path = config.installations_config_path
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise InstallationsConfigError(f"{path} could not be parsed: {exc}") from exc
    instances = []
    for entry in data.get("instance", []):
        try:
            instances.append(
                Instance(
                    name=str(entry["name"]),
                    host=str(entry.get("host", "127.0.0.1")),
                    port=int(entry["port"]),
                    prefix=str(entry.get("prefix", "")),
                    token=str(entry["token"]),
                    tunnel_host=(str(entry["tunnel_host"]) if "tunnel_host" in entry else None),
                    tunnel_port=(int(entry["tunnel_port"]) if "tunnel_port" in entry else None),
                    stripped=bool(entry.get("stripped", False)),
                )
            )
        except KeyError as exc:
            raise InstallationsConfigError(
                f"{path}: an [[instance]] entry is missing required field {exc}"
            ) from exc
    return instances


async def _check_health(instance: Instance, client: httpx.AsyncClient) -> dict[str, object]:
    """Async on purpose: an instance's own admin page checking its own
    /health is a self-request on the same event loop. A blocking client
    here would stall waiting for a response that can't be produced until
    the loop is free — deadlocking until the timeout, which then reads as
    "stopped" even though the process is fine.
    """
    local_prefix = "" if instance.stripped else instance.prefix
    url = f"http://{instance.host}:{instance.port}{local_prefix}/health"
    try:
        response = await client.get(url, timeout=_HEALTH_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {"running": False}
    return {
        "running": True,
        "version": str(payload.get("version", "?")),
        "started_at": str(payload.get("started_at", "?")),
    }


def _open_url(instance: Instance) -> str:
    """The launcher link: prefer the instance's own declared tunnel host
    (set in installations.toml) so the link works when the admin page
    itself is viewed remotely; fall back to loopback only when no tunnel
    host is configured for that instance.
    """
    if instance.tunnel_host is None:
        local_prefix = "" if instance.stripped else instance.prefix
        return f"http://{instance.host}:{instance.port}{local_prefix}/ui/{instance.token}/"
    authority = (
        instance.tunnel_host
        if instance.tunnel_port is None
        else f"{instance.tunnel_host}:{instance.tunnel_port}"
    )
    return f"https://{authority}{instance.prefix}/ui/{instance.token}/"


def _instance_row(instance: Instance, health: dict[str, object]) -> str:
    open_url = _open_url(instance)
    if health["running"]:
        status_html = '<span class="status up">running</span>'
        meta = f"v{_e(str(health['version']))} · since {_e(str(health['started_at']))}"
    else:
        status_html = '<span class="status down">stopped</span>'
        local_prefix = "" if instance.stripped else instance.prefix
        meta = _e(f"{instance.host}:{instance.port}{local_prefix or '/'}")
    return (
        '<div class="instance-row">'
        f'<span class="name">{_e(instance.name)}</span>'
        f'<span class="meta">{meta}</span>'
        f"{status_html}"
        f'<a class="open" href="{_e(open_url)}">Open →</a>'
        "</div>"
    )


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Installations</title>\n"
        f"<style>\n{_ADMIN_CSS}</style>\n"
        f'<div class="admin">\n{body}\n</div>\n'
    )


def _authorized(request: Request) -> Config | None:
    config = load_config()
    token_path = config.data_dir / _ADMIN_TOKEN_FILENAME
    if not token_path.exists():
        return None
    expected = token_path.read_text(encoding="utf-8").strip()
    presented = str(request.path_params.get("token", ""))
    if not secrets.compare_digest(presented, expected):
        return None
    return config


async def installations_page(request: Request) -> Response:
    config = _authorized(request)
    if config is None:
        return Response("not found", status_code=404)
    try:
        instances = load_instances(config)
    except InstallationsConfigError as exc:
        return _page(f'<h1>Installations</h1><div class="empty">{_e(str(exc))}</div>')
    if not instances:
        return _page(
            "<h1>Installations</h1>"
            f'<div class="empty">No instances configured. Add <code>[[instance]]</code> '
            f"entries to <code>{_e(str(config.installations_config_path))}</code>.</div>"
        )
    async with httpx.AsyncClient() as client:
        healths = await asyncio.gather(*(_check_health(instance, client) for instance in instances))
    rows = "".join(_instance_row(instance, health) for instance, health in zip(instances, healths))
    body = "<h1>Installations</h1>" + f'<div class="instances">{rows}</div>'
    return _page(body)


_registered = False


def register_admin(server: FastMCP) -> None:
    """Mount the installations page. Idempotent — safe to call once per
    process regardless of how many instances this process itself serves.
    """
    global _registered
    if _registered:
        return
    _registered = True
    server.custom_route("/admin/{token}/installations", methods=["GET"])(installations_page)
