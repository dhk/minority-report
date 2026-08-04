"""Version reporting, resolved from the installed package metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

_DIST_NAME = "example-service"


def service_version() -> str:
    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return "0.0.0+dev"
