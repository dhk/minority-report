#!/usr/bin/env python3
"""Run Minority Report's code-quality checks.

Split out of dhk/alexandria's scripts/validate.py (see
https://github.com/dhk/alexandria/issues/33): that script used to also parse
every tracked JSON/TOML/YAML artifact in the repo, because src/ and
research/ shared one checkout. This repo has no research/ artifacts to
parse — that half of the check now lives in dhk/alexandria's own
scripts/validate.py. This half — lint, types, tests — stays here, against
src/ and tests/.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str) -> None:
    """Run one validation command from the repository root."""
    executable = command[0]
    if shutil.which(executable) is None:
        raise RuntimeError(
            f"{executable!r} is unavailable; run this validator with "
            "`uv run --frozen python scripts/validate.py`"
        )

    print(f"\n==> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    try:
        run("ruff", "check", ".")
        run("ruff", "format", "--check", "src", "tests", "scripts")
        run("mypy", "src", "tests")
        run("pytest")
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"\nvalidation failed: {exc}", file=sys.stderr)
        return 1

    print("\nValidation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
