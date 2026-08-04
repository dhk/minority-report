"""API/provider keys: a host file, then a workspace file — one resolution ladder.

Mirrors wingman's `infrastructure/keys.py` (RFC-019/RFC-034 there), minus the
macOS Keychain and provider-specific live-test calls — add those back in the
forked project if it needs them (Keychain is optional; the file ladder below
is the part every service on a shared Linux host actually depends on).

Resolution order, first match wins: an already-set environment variable,
then the host's canonical key file (`~/.config/<service>.env` — the
"wingman.env" pattern: one file every consumer on a host reads, instead of
shell exports from one setup session and a systemd EnvironmentFile from
another silently drifting apart), then this service's own workspace key
file (`keys.env` inside its data dir, e.g. written by a future onboarding
form). Secrets are never logged and never returned by any status/listing
function here — only which source they came from.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Canonical short names -> the environment variable each key hydrates.
# Replace with the real providers this service actually calls.
KNOWN_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# Rename to match APP_NAME in config.py (e.g. "alexandria.env").
HOST_KEY_FILENAME = "example_service.env"
HOST_KEY_SUBDIR = ".config"
WORKSPACE_KEY_FILENAME = "keys.env"


def _parse_known_keys_file(path: Path) -> dict[str, str]:
    """NAME=value lines from an env-shaped file; unknown names ignored."""
    if not path.exists():
        return {}
    known = set(KNOWN_KEYS.values())
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() in known and value.strip():
            values[name.strip()] = value.strip()
    return values


def host_keys_path(home: Path | None = None) -> Path:
    """The one canonical host key file, 0600, shared by every consumer on the host."""
    return (home if home is not None else Path.home()) / HOST_KEY_SUBDIR / HOST_KEY_FILENAME


def read_host_keys(home: Path | None = None) -> dict[str, str]:
    return _parse_known_keys_file(host_keys_path(home))


def workspace_keys_path(data_dir: Path) -> Path:
    return data_dir / WORKSPACE_KEY_FILENAME


def read_workspace_keys(data_dir: Path) -> dict[str, str]:
    return _parse_known_keys_file(workspace_keys_path(data_dir))


def ensure_env(data_dir: Path | None = None, home: Path | None = None) -> list[str]:
    """Hydrate absent env vars from the host file, then the workspace file.

    An already-set environment variable always wins. Returns the hydrated
    variable names. Safe to call anywhere, any number of times — call it
    once at process startup (CLI entrypoint, MCP server main()).
    """
    hydrated: list[str] = []
    for env_var, value in read_host_keys(home).items():
        if os.environ.get(env_var, "").strip():
            continue
        os.environ[env_var] = value
        hydrated.append(env_var)
    if data_dir is not None:
        for env_var, value in read_workspace_keys(data_dir).items():
            if os.environ.get(env_var, "").strip():
                continue
            os.environ[env_var] = value
            hydrated.append(env_var)
    return hydrated


_SOURCE_LABELS = (
    "environment",
    f"host file (~/{HOST_KEY_SUBDIR}/{HOST_KEY_FILENAME})",
    "workspace file",
)


@dataclass(frozen=True)
class KeySource:
    """Where one known key was found, and whether other sources disagree."""

    short_name: str
    env_var: str
    winning_source: str
    shadowed_by: list[str] = field(default_factory=list)


def resolve_key_sources(
    environ: Mapping[str, str],
    data_dir: Path | None = None,
    home: Path | None = None,
) -> list[KeySource]:
    """Per known key: which source wins, and which others disagree with a
    different value. Read-only — pass an environ snapshot taken BEFORE
    ensure_env() hydrates it, or "winning source" degenerates to always
    "environment" once hydration has copied a file value into os.environ.
    """
    host_values = read_host_keys(home)
    workspace_values = read_workspace_keys(data_dir) if data_dir is not None else {}
    results: list[KeySource] = []
    for short_name, env_var in KNOWN_KEYS.items():
        candidates: list[tuple[str, str]] = []
        env_value = environ.get(env_var, "").strip()
        if env_value:
            candidates.append((_SOURCE_LABELS[0], env_value))
        if env_var in host_values:
            candidates.append((_SOURCE_LABELS[1], host_values[env_var]))
        if env_var in workspace_values:
            candidates.append((_SOURCE_LABELS[2], workspace_values[env_var]))
        if not candidates:
            results.append(KeySource(short_name, env_var, "not set"))
            continue
        winning_source, winning_value = candidates[0]
        shadowed_by = [source for source, value in candidates[1:] if value != winning_value]
        results.append(KeySource(short_name, env_var, winning_source, shadowed_by))
    return results
