"""Configuration loading: two separate paths, resolved separately.

`data_dir` is where THIS SERVER's own local state lives (capability
token, pidfile, installations.toml) — small, disposable, never
version-controlled. `repo_root` is the Alexandria git checkout the server
reads research/ from — the durable system of record (docs/DESIGN.md).
They must not be conflated: the server's own token has no business inside
the repository it's reading, and the repository has no business inside
platformdirs' scratch space.

Follows the same pattern as templates/mcp-server/, forked with Alexandria's
own APP_NAME plus the added repo_root concept a pure local-state service
doesn't need.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import BaseModel

APP_NAME = "alexandria"
ENV_DATA_DIR = "ALEXANDRIA_DATA_DIR"
ENV_REPO_ROOT = "ALEXANDRIA_REPO"

# Files that mark a directory as an Alexandria checkout, cheaply and
# without importing git — good enough to disambiguate from an arbitrary
# cwd without claiming to validate the whole repository contract.
_MARKER_FILES = ("docs/DESIGN.md", "AGENTS.md")


class RepoNotFoundError(Exception):
    """No Alexandria checkout found, and none was configured."""


class Config(BaseModel):
    data_dir: Path
    data_dir_source: str
    repo_root: Path
    repo_root_source: str

    @property
    def research_dir(self) -> Path:
        return self.repo_root / "research"

    @property
    def installations_config_path(self) -> Path:
        return self.data_dir / "installations.toml"


def _looks_like_repo_root(path: Path) -> bool:
    return all((path / marker).is_file() for marker in _MARKER_FILES)


def _find_repo_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if _looks_like_repo_root(candidate):
            return candidate
    return None


def load_config(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Config:
    """Resolve local-state dir and repo root independently.

    data_dir: ALEXANDRIA_DATA_DIR if set, else the platform user data dir.
    repo_root: ALEXANDRIA_REPO if set, else detected by walking up from
    `cwd` (default: the process's actual cwd) looking for the marker
    files above. Raises RepoNotFoundError if neither works — every tool
    that needs the repo should catch this and return a clear message
    rather than let it propagate as a traceback.
    """
    environment = os.environ if env is None else env
    cwd = cwd if cwd is not None else Path.cwd()

    data_override = environment.get(ENV_DATA_DIR, "").strip()
    if data_override:
        data_dir = Path(data_override).expanduser()
        data_dir_source = f"{ENV_DATA_DIR} environment variable"
    else:
        data_dir = Path(user_data_dir(APP_NAME))
        data_dir_source = "platform user data directory"

    repo_override = environment.get(ENV_REPO_ROOT, "").strip()
    if repo_override:
        repo_root = Path(repo_override).expanduser()
        repo_root_source = f"{ENV_REPO_ROOT} environment variable"
    else:
        found = _find_repo_root(cwd)
        if found is None:
            raise RepoNotFoundError(
                f"No Alexandria checkout found from {cwd}. Set {ENV_REPO_ROOT} "
                "to your checkout path (e.g. in ~/.config/alexandria.env)."
            )
        repo_root = found
        repo_root_source = f"detected from {cwd}"

    return Config(
        data_dir=data_dir,
        data_dir_source=data_dir_source,
        repo_root=repo_root,
        repo_root_source=repo_root_source,
    )
