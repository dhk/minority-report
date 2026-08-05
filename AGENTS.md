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
