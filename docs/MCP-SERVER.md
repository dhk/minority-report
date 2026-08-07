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

## Tools and spend gate

`status`, `list_research`, `show_research`, and `search_research` read the corpus.
`begin_research` resolves inputs, prices the draft, and returns an exact
`RUN <draft-id>` confirmation without dispatching research models.
`run_research` is the spend boundary and accepts only that draft-specific phrase.

The server drafts and validates; it does not commit to Alexandria. HTTP access is
spend-capable, so expose it only through reviewed host configuration and never
publish the capability URL.

## Lifecycle

For an installed tool, `alexandria-ctl status`, `start`, `stop-all`, `cycle`, and
`url` manage or inspect the local service. `cycle` upgrades from the Minority
Report checkout selected with `--repo`; `ALEXANDRIA_REPO` still selects the
different corpus checkout used by the server.
