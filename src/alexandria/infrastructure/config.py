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
import shlex
from collections.abc import Mapping
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import BaseModel

APP_NAME = "alexandria"
ENV_DATA_DIR = "ALEXANDRIA_DATA_DIR"
ENV_REPO_ROOT = "ALEXANDRIA_REPO"
ENV_SECRETS_FILE = "ALEXANDRIA_SECRETS_FILE"
DEFAULT_HOST_ENV_FILE = Path("~/.config/alexandria/alexandria.env")
HOST_ENV_NAMES = frozenset({ENV_DATA_DIR, ENV_REPO_ROOT, ENV_SECRETS_FILE})

# Files that mark a directory as an Alexandria checkout, cheaply and
# without importing git — good enough to disambiguate from an arbitrary
# cwd without claiming to validate the whole repository contract.
_MARKER_FILES = ("docs/DESIGN.md", "AGENTS.md")


class HostEnvironmentError(RuntimeError):
    """The canonical host environment file is present but invalid."""


class RepoNotFoundError(RuntimeError):
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


def _parse_env_value(value: str, *, path: Path, line_number: int) -> str:
    try:
        parsed = shlex.split(value, comments=False, posix=True)
    except ValueError as exc:
        raise HostEnvironmentError(f"{path}:{line_number}: invalid quoted value: {exc}") from exc
    if len(parsed) != 1:
        raise HostEnvironmentError(
            f"{path}:{line_number}: values containing whitespace must be quoted"
        )
    return parsed[0]


def read_host_environment(path: Path) -> dict[str, str]:
    """Read recognized host settings as data, without executing the file."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HostEnvironmentError(f"cannot read {path}: {exc}") from exc
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise HostEnvironmentError(f"{path}:{line_number}: expected NAME=value")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if name not in HOST_ENV_NAMES:
            continue
        value = _parse_env_value(raw_value.strip(), path=path, line_number=line_number)
        if not value:
            raise HostEnvironmentError(f"{path}:{line_number}: {name} may not be empty")
        values[name] = value
    return values


def resolve_host_environment(
    env: Mapping[str, str] | None = None,
    *,
    host_env_file: Path | None = None,
) -> tuple[dict[str, str], frozenset[str], Path]:
    """Merge process settings over the canonical, non-secret host config."""
    environment = dict(os.environ if env is None else env)
    path = (host_env_file or DEFAULT_HOST_ENV_FILE).expanduser()
    from_file: set[str] = set()
    for name, value in read_host_environment(path).items():
        if environment.get(name, "").strip():
            continue
        environment[name] = value
        from_file.add(name)
    return environment, frozenset(from_file), path


def load_config(
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    *,
    host_env_file: Path | None = None,
) -> Config:
    """Resolve local-state dir and repo root independently.

    Process environment values take precedence over recognized values in
    ``~/.config/alexandria/alexandria.env``. ``data_dir`` then falls back to
    the platform user data dir; ``repo_root`` falls back to walking up from
    ``cwd`` (default: the process's actual cwd) looking for the marker files.
    Raises RepoNotFoundError if neither repository source works.
    """
    environment, from_file, resolved_host_file = resolve_host_environment(
        env, host_env_file=host_env_file
    )
    cwd = cwd if cwd is not None else Path.cwd()

    data_override = environment.get(ENV_DATA_DIR, "").strip()
    if data_override:
        data_dir = Path(data_override).expanduser()
        data_dir_source = (
            f"{ENV_DATA_DIR} in {resolved_host_file}"
            if ENV_DATA_DIR in from_file
            else f"{ENV_DATA_DIR} environment variable"
        )
    else:
        data_dir = Path(user_data_dir(APP_NAME))
        data_dir_source = "platform user data directory"

    repo_override = environment.get(ENV_REPO_ROOT, "").strip()
    if repo_override:
        repo_root = Path(repo_override).expanduser()
        repo_root_source = (
            f"{ENV_REPO_ROOT} in {resolved_host_file}"
            if ENV_REPO_ROOT in from_file
            else f"{ENV_REPO_ROOT} environment variable"
        )
    else:
        found = _find_repo_root(cwd)
        if found is None:
            raise RepoNotFoundError(
                f"No Alexandria checkout found from {cwd}. Set {ENV_REPO_ROOT} "
                f"to your checkout path (e.g. in {resolved_host_file})."
            )
        repo_root = found
        repo_root_source = f"detected from {cwd}"

    return Config(
        data_dir=data_dir,
        data_dir_source=data_dir_source,
        repo_root=repo_root,
        repo_root_source=repo_root_source,
    )
