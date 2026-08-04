"""Configuration loading: where this service's local state lives on disk.

Mirrors wingman's `infrastructure/config.py` (see AGENTS.md's routing map —
this pattern is exactly what the services-repo issue proposes extracting).
Rename APP_NAME and ENV_DATA_DIR when forking this template.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import BaseModel

# Rename both of these for the forked project (e.g. APP_NAME = "alexandria",
# ENV_DATA_DIR = "ALEXANDRIA_DATA_DIR").
APP_NAME = "example_service"
ENV_DATA_DIR = "EXAMPLE_SERVICE_DATA_DIR"


class Config(BaseModel):
    """Resolved local-state configuration.

    `data_dir` is where THIS SERVICE's own state lives (capability token,
    pidfile, installations.toml) — never assumed to be the project's own
    source checkout or any data repository the service reads from. A
    project with its own durable store (a database, a git repo it treats
    as system of record, ...) should add that as a separate resolved path,
    not conflate it with this one.
    """

    data_dir: Path
    data_dir_source: str

    @property
    def installations_config_path(self) -> Path:
        return self.data_dir / "installations.toml"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Resolve the data directory: ENV_DATA_DIR if set, else the platform user data dir.

    Never defaults to a path relative to the invoking directory — an
    installed CLI/service must not scatter state wherever it happens to be
    run from.
    """
    environment = os.environ if env is None else env
    override = environment.get(ENV_DATA_DIR, "").strip()
    if override:
        return Config(
            data_dir=Path(override).expanduser(),
            data_dir_source=f"{ENV_DATA_DIR} environment variable",
        )
    return Config(
        data_dir=Path(user_data_dir(APP_NAME)),
        data_dir_source="platform user data directory",
    )
