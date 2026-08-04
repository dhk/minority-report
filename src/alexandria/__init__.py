"""Alexandria: version control for research — the MCP server package.

This package exposes read/status tools over the research/ tree that the
rest of the repository treats as the system of record. It does not
generate, dispatch, or synthesize research; see docs/orchestration-harness.md
for that separate, not-yet-built concern.
"""

from alexandria.version import service_version

__all__ = ["service_version"]
