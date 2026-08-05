# Instructions for Claude sessions in this repository

Engineering rules, invariants, and working method live in
[`AGENTS.md`](AGENTS.md) — read it first.

## What this repository is

Minority Report is the tooling. The research corpus is a separate repository,
[dhk/alexandria](https://github.com/dhk/alexandria), and it — not this code and
not any running service — is the authoritative record. Point `ALEXANDRIA_REPO`
at a checkout of it.

## Deployment facts (owner directives)

- **Mac** — the owner's laptop. **Global convention: every repo checkout lives
  under `~/Documents/dev`** (e.g. `~/Documents/dev/wingman`,
  `~/Documents/dev/minority-report`) — not specific to this repo, applies
  everywhere.
- **Lobster** — the Ubuntu host. Services install as pack bundles under
  `~/src/<tool>/` with a `current` symlink into `releases/`, run as systemd
  *user* units, and are reached through a Tailscale funnel that mounts each
  service at its own path prefix. The prefix is part of the connector URL; a
  URL without it lands on a different service.

## Working with other sessions

More than one session may be working across these repositories at once. Before
changing shared paths, check what is already in flight — open PRs in both repos,
and recent commits on `main`. `AGENTS.md` rule 10 applies with particular force
to anything you are about to tell another session is done, absent, or current.
