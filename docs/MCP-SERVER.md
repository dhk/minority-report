# MCP server

`alexandria-mcp` exposes read-only corpus recall and an explicitly gated
commission flow from the Minority Report codebase.

## Configure

```bash
export ALEXANDRIA_REPO=/absolute/path/to/alexandria
uv sync --frozen
```

Provider-backed calls also require `OPENROUTER_API_KEY` in the environment or the
local file selected by `ALEXANDRIA_SECRETS_FILE`. HTTP capability tokens and
provider keys are secrets: do not paste them into issues, logs, run artifacts, or
documentation.

## Run

```bash
uv run alexandria-mcp
uv run alexandria-mcp --http --port 8797 --tunnel-path /alexandria
```

The first form is stdio for an MCP client. The second binds loopback by default
and serves streamable HTTP under a generated capability path. Use
`--rotate-token` with `--http` to revoke the existing capability token.

Claude Code example from the Minority Report checkout:

```bash
claude mcp add alexandria --scope user -- \
  uv run --project /absolute/path/to/minority-report alexandria-mcp
```

Ensure the client process receives `ALEXANDRIA_REPO`; the project path above is
the executable checkout, not the corpus.

## Tools

Ten tools in three groups.

**Corpus recall** — read `research/`, deterministic, no spend.

| Tool | |
|---|---|
| `status()` | Investigation counts by lifecycle stage and assurance level. |
| `list_research(assurance="", stage="")` | Investigations under `research/`, optionally filtered. |
| `show_research(slug)` | One investigation's `topic.yaml` fields, README, and stage checklist. |
| `search_research(query, limit=10)` | Case-insensitive substring search with file/line citations. |

**Commissioning** — the only path that spends.

| Tool | |
|---|---|
| `begin_research(task, ...)` | Resolves inputs, prices the draft, returns an exact `RUN <draft-id>` phrase. Dispatches nothing. |
| `run_research(draft_id, confirmation="")` | The spend boundary. Accepts only that draft-specific phrase. |

**Runs and resolution** — inspect what was commissioned, and draft results back.

| Tool | |
|---|---|
| `list_runs(limit=20)` | Commissioned runs on this host, newest first: id, status, cost, models. |
| `run_status(run_id)` | What happened to one run, by id. |
| `publish_run(run_id, slug, title="", overwrite=False)` | Draft a finished run into the corpus working tree as investigation `slug`. |
| `draft_resolution(slug, outcome, expression="", ...)` | Validate an idea's resolution and return the `resolution.yaml` text to save. |

### Runs are not corpus

`list_runs` and `run_status` read the **local run store**, not the committed
corpus. `status` and `list_research` will not show a run until someone
deliberately promotes it, so they are not a signal about a run that just finished
— or one still going.

`run_status` exists for a specific failure: a caller who lost `run_research`'s
reply to a client-side timeout can still find out whether the run happened, what
it cost, and which models answered. The work continues on the server regardless
of whether anyone is still listening.

### Resolution outcomes

`draft_resolution` accepts exactly three outcomes — `implemented`, `morphed`, or
`nixed`. There is deliberately no fourth "back-burnered" value: an idea with no
`resolution.yaml` is simply unresolved. `morphed` requires `expression`, a forward
pointer to what the idea became; morphed without one is itself a dead end and is
rejected.

## The write boundary

**Every write-shaped tool here drafts and stops.** `publish_run` writes into the
corpus working tree and does not commit or push. `draft_resolution` returns file
text and touches nothing at all. The corpus is authoritative because a human puts
things there — read the diff, then commit it yourself.

`publish_run` does not publish raw provider responses, because the corpus is
public. The manifest records that they exist, with hashes, so the omission is
visible rather than silent. Extracted quotes in `scores.csv` do publish.

HTTP access is spend-capable: expose it only through reviewed host configuration,
and never publish the capability URL.

## Lifecycle

For an installed tool, `alexandria-ctl status`, `start`, `stop-all`, `cycle`, and
`url` manage or inspect the local service. `cycle` upgrades from the Minority
Report checkout selected with `--repo`; `ALEXANDRIA_REPO` still selects the
different corpus checkout used by the server.
