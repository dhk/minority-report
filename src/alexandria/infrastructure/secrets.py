"""Resolve provider credentials without exposing them to the UI or repository."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from alexandria.infrastructure.config import (
    ENV_SECRETS_FILE,
    resolve_host_environment,
)

ENV_OPENROUTER_KEY = "OPENROUTER_API_KEY"
ENV_GITHUB_TOKEN = "GITHUB_TOKEN"
DEFAULT_SECRETS_FILE = Path("~/.config/alexandria/secrets.env")


class SecretNotFoundError(RuntimeError):
    """A required local provider credential could not be resolved."""


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def _secrets_path(environment: Mapping[str, str]) -> Path:
    configured = environment.get(ENV_SECRETS_FILE, str(DEFAULT_SECRETS_FILE))
    return Path(configured).expanduser()


def _lookup(name: str, environment: Mapping[str, str]) -> str:
    """Resolve one credential: environment first, then the local secrets file."""
    direct = environment.get(name, "").strip()
    if direct:
        return direct
    return _read_env_file(_secrets_path(environment)).get(name, "").strip()


def openrouter_api_key(
    env: Mapping[str, str] | None = None,
    *,
    host_env_file: Path | None = None,
) -> str:
    """Return the OpenRouter key from env or the configured local secrets file."""
    environment, _, _ = resolve_host_environment(env, host_env_file=host_env_file)
    key = _lookup(ENV_OPENROUTER_KEY, environment)
    if key:
        return key
    raise SecretNotFoundError(
        f"Set {ENV_OPENROUTER_KEY} in the environment or in {_secrets_path(environment)}."
    )


def github_token(
    env: Mapping[str, str] | None = None,
    *,
    host_env_file: Path | None = None,
) -> str | None:
    """Return the GitHub token if one is configured, else None.

    Unlike the OpenRouter key this is optional: public GitHub URLs resolve
    unauthenticated, so a missing token is a normal state and never an error.
    """
    environment, _, _ = resolve_host_environment(env, host_env_file=host_env_file)
    return _lookup(ENV_GITHUB_TOKEN, environment) or None
