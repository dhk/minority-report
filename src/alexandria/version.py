"""Version reporting, resolved from the installed package metadata."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

_DIST_NAME = "alexandria"


def service_version() -> str:
    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return "0.0.0+dev"


def deployed_release() -> dict[str, Any] | None:
    """The release this code was installed from, or None if it was not.

    PEP 610 records the directory a distribution was installed from in
    ``direct_url.json``. For a pack install that directory is the release, and
    the release carries the manifest naming its bundle id and source commit. So
    the running process can say exactly which build is answering rather than
    only its package version, which never changes.

    Returns None for a source checkout, an editable install, or a release whose
    manifest is missing — all real states, none of them errors.
    """
    try:
        provenance = distribution(_DIST_NAME).read_text("direct_url.json")
        if not provenance:
            return None
        url = json.loads(provenance).get("url", "")
        if not url.startswith("file://"):
            return None
        release = Path(url2pathname(urlparse(url).path))
        manifest = json.loads((release / ".pack-manifest.json").read_text())
    except (PackageNotFoundError, OSError, ValueError, KeyError):
        return None
    return manifest if isinstance(manifest, dict) else None


def deployed_summary() -> str:
    """One line naming the build that is answering, for status output."""
    manifest = deployed_release()
    if manifest is None:
        return f"{service_version()} (not a pack install)"
    commit = (manifest.get("source") or {}).get("commit") or "?"
    return f"{manifest.get('bundle_id', service_version())} from {commit[:12]}"
