# AGENTS.md

## Repository purpose

Minority Report is the orchestration tooling for multi-model research: the MCP
server, the commission dispatch and grading flow, the local web surface, and
the deploy/packaging pipeline. It reads and writes the research corpus, which
lives separately in [dhk/alexandria](https://github.com/dhk/alexandria) and is
the authoritative record — this repository is not.

The name is literal, not decorative. The point of the system is to surface
disagreement, silence, and outlier claims across models. Code that quietly
smooths those away is working against the product.

## Working rules

1. Never commit substantive work directly to `main`.
2. Create a branch named `agent/<short-description>`, `fix/<short-description>`,
   or `feat/<short-description>`.
3. Keep each pull request focused on one coherent change.
4. Preserve raw provider responses exactly as received. `CallRecord.raw_response`
   stores the body verbatim; tolerance for malformed input belongs at decode
   time, never at capture time.
5. A failed or unpriced provider call is a recorded observation, not an absence.
   Never drop one from a cost total, a claim table, or a run record — that is
   how an overrun or a missing model becomes invisible.
6. Do not treat model agreement as factual validation, and do not write code
   that presents it as such.
7. This repository never writes to the corpus's `research/` tree. Tools may
   validate and draft artifacts; saving and committing them is the operator's
   deliberate act.
8. Do not commit API keys, credentials, capability tokens, private session
   data, or hidden reasoning. Tokens must not be logged, persisted to a run
   record, or rendered in any surface.
9. Every user-facing surface must state what it does not cover. An estimate is
   not a cap; a healthy check is not a working path; a green run with a failed
   model is not a three-model run.
10. **Verify the claim before you make it.** Any assertion that something is
    absent, identical, complete, current, or the only copy — in a handoff, a PR
    body, a commit message, or a report — must be checked by a command before
    it is written, and the check named alongside it. Report what the command
    returned, not what you expect it to return. This rule exists because an
    unverified "this exists only here" nearly cost merged work in this
    repository, and because a diff that compared a tree against itself was
    reported as a passing verification gate.
11. Keep code and its tests in the same change. A behaviour that arrives without
    a test is not finished, including behaviour ported from elsewhere.
12. When a change alters how artifacts are read or written, coordinate the
    matching contract change in `dhk/alexandria`.

## Pull-request expectations

Every pull request should state:

- what changed;
- why it changed;
- which artifacts or contracts are affected;
- how it was validated — and what was *not* validated;
- whether existing analyses become stale;
- any unresolved design decisions.

State the limits of a fix as plainly as the fix. A change that makes a problem
measurable without solving it should say so in those words.

## Cross-repository handoff

Work moves between repositories and between sessions as a **branch**, never as
an archive of loose files. A branch carries its base commit, so a reviewer can
see what it was written against; a tarball carries nothing, and reconciling one
has already cost more than the work inside it — and concealed a regression
against current `main`.

Before handing off, run the diff that would falsify your summary of it.

## Deployment

`deploy/pack.toml` plus `deploy/install.py` build a self-installing bundle. The
install root holds the code; `ALEXANDRIA_REPO` names the corpus checkout, which
is a *different repository* and must never be pointed at the install root. See
`repo_is_install_root` in `deploy/pack.toml`.

## Validation

`uv run --frozen python scripts/validate.py` runs ruff, mypy, and the test
suite. CI runs the same script; a green local run and a green CI run are not
the same evidence, and neither is a substitute for exercising the code.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
