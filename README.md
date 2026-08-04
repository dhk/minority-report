# Minority Report

The orchestration tooling for [Alexandria](https://github.com/dhk/alexandria):
the MCP server, the commission web surface, and the deploy/packaging
machinery that dispatch a research brief to independent models, preserve
their raw responses, and grade the resulting claim landscape.

Alexandria itself is now just the corpus — `research/`, `docs/`, `schemas/`
— the durable, git-backed system of record. This repo is the tool that
reads and writes it. Models and dispatch logic can keep changing here
without touching that evidence trail's history.

Split out of `dhk/alexandria` per
[dhk/alexandria#33](https://github.com/dhk/alexandria/issues/33). `src/alexandria/`
was moved with its git history intact (`git subtree split`); `deploy/`,
`templates/mcp-server/`, `tests/`, and `scripts/pack.py` were moved as
plain commits.

## Status: split in progress, not yet verified end-to-end

This is the first pass at the split — code has moved, nothing has been run
against a real corpus checkout yet. Known follow-ups:

- **Package and command names are unchanged for now.** The Python package is
  still `alexandria`, and the console scripts are still `alexandria-mcp`,
  `alexandria-web`, `alexandria-ctl`. Renaming to match "Minority Report" is
  a deliberate follow-up, not done here, to keep this first split to a
  file-boundary change rather than a rename-everything change.
- **`uv sync` / `uv run --frozen python scripts/validate.py` have not been
  re-run against this tree yet.** `uv.lock` was copied over from the
  pre-split repo as a starting point and may need regenerating.
- **The cross-repo write path is still open** — how this server's write
  side (once it has one beyond the commission run record) commits into
  Alexandria's `research/` tree across the repo boundary. Tracked as open
  question #3 on issue #33.
- **`.github/workflows/ci.yml`** was copied as-is from the pre-split repo
  and has not been confirmed green here.

## Pointing this at your Alexandria checkout

The config layer already anticipated a checkout that isn't co-located with
`research/` — `ALEXANDRIA_REPO` (see `.env.example`) has always been the
explicit override; auto-detection just walks up from the process's cwd
looking for `docs/DESIGN.md` + `AGENTS.md`, which now only live in the
`alexandria` corpus checkout, not here. In practice, **post-split you
almost always want `ALEXANDRIA_REPO` set explicitly** rather than relying
on auto-detection, unless this server happens to be invoked with its cwd
inside an `alexandria` checkout.

```bash
export ALEXANDRIA_REPO=/path/to/your/alexandria/checkout
uv sync
uv run alexandria-mcp
```

## What's in here

```text
src/alexandria/     MCP server, commission dispatch, web surface, control CLI
deploy/              Packaging, systemd install, host service registry
templates/mcp-server/  Project-agnostic MCP server scaffold this was forked from
scripts/             validate.py (lint/type/test) and pack.py (release bundles)
tests/               Unit tests for all of the above
```

See [dhk/alexandria](https://github.com/dhk/alexandria) for the research
corpus this tooling operates on, and its `docs/` for the architecture this
tool implements (`docs/MCP-SERVER.md`, `docs/COMMISSION-SURFACE.md`,
`docs/PACKAGING.md`, `docs/RFC-0006-host-service-registry.md`).
