"""Resolve provider credentials without exposing them to the UI or repository."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from alexandria.infrastructure.config import (
    ENV_SECRETS_FILE,
    resolve_host_environment,
)

ENV_OPENROUTER_KEY = "OPENROUTER_API_KEY"
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


def openrouter_api_key(
    env: Mapping[str, str] | None = None,
    *,
    host_env_file: Path | None = None,
) -> str:
    """Return the OpenRouter key from env or the configured local secrets file."""
    environment, _, _ = resolve_host_environment(env, host_env_file=host_env_file)
    direct = environment.get(ENV_OPENROUTER_KEY, "").strip()
    if direct:
        return direct

    configured = environment.get(ENV_SECRETS_FILE, str(DEFAULT_SECRETS_FILE))
    secrets_path = Path(configured).expanduser()
    key = _read_env_file(secrets_path).get(ENV_OPENROUTER_KEY, "").strip()
    if key:
        return key
    raise SecretNotFoundError(f"Set {ENV_OPENROUTER_KEY} in the environment or in {secrets_path}.")
