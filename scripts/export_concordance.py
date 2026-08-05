#!/usr/bin/env python3
"""Regenerate the corpus's generated/concordance-feed.json (dhk/alexandria#36).

Generated, derived data -- AGENTS.md's "Generated files: Generated indexes
may be rebuilt" applies directly. Run this, review the diff, and commit it
like any other change; nothing writes this file for you.

The feed is corpus data and belongs in the corpus repo, but the code that
builds it is tooling and lives here. So the output path is resolved from the
configured corpus root (ALEXANDRIA_REPO), not from this repo -- before the
split those were the same directory and ``parents[1]`` was correct; now they
are different repositories and it would silently write to the wrong one.

    ALEXANDRIA_REPO=/path/to/alexandria \\
        uv run --frozen python scripts/export_concordance.py

This still leaves the *committing* half of the cross-repo write path open
(dhk/alexandria#33, open question 2): the file lands in a corpus checkout,
and getting it from there onto a branch and into a PR is still manual.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alexandria.concordance import build_concordance_feed, feed_to_json
from alexandria.infrastructure.config import load_config


def main() -> int:
    config = load_config()
    output_path = config.repo_root / "generated" / "concordance-feed.json"
    feed = build_concordance_feed(config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(feed_to_json(feed), indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(feed.entries)} concordance entries to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
