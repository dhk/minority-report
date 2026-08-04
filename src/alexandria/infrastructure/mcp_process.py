"""Pidfile management for the --http server process (see templates/mcp-server
for the generic version this was forked from)."""

from __future__ import annotations

import os
import signal
from pathlib import Path

from alexandria.infrastructure.config import Config

_PIDFILE_NAME = "mcp-http.pid"


def _pidfile_path(config: Config) -> Path:
    return config.data_dir / _PIDFILE_NAME


def write_pidfile(config: Config) -> None:
    path = _pidfile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def read_server_pid(config: Config) -> int | None:
    """The pid recorded in the pidfile, or None if absent/stale."""
    path = _pidfile_path(config)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        return pid
    return pid


def clear_pidfile(config: Config) -> None:
    _pidfile_path(config).unlink(missing_ok=True)


def stop_server(config: Config) -> bool:
    pid = read_server_pid(config)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_pidfile(config)
        return False
    return True
