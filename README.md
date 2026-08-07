# Minority Report

Minority Report is the executable orchestration system for the
[Alexandria](https://github.com/dhk/alexandria) research corpus. It owns provider
adapters, commission dispatch and grading, local run records, MCP and web
surfaces, packaging, deployment, and host operations.

Alexandria separately owns durable evidence, lifecycle, schemas, policy,
provenance, publication, and research governance. A local run here is not an
Alexandria corpus artifact until an operator deliberately reviews and promotes
it.

## Clone, configure, and validate

Clone both repositories as siblings or at any two explicit paths:

```bash
git clone https://github.com/dhk/alexandria.git
git clone https://github.com/dhk/minority-report.git
cd minority-report
uv sync --frozen
export ALEXANDRIA_REPO=/absolute/path/to/alexandria
uv run --frozen python scripts/validate.py
```

`ALEXANDRIA_REPO` must name the live corpus checkout, never this repository, a
packaging build tree, or an installed release. For provider-backed runs, put
`OPENROUTER_API_KEY` in the process environment or the configured local secrets
file. Never commit that file or print its contents.

The installed Python distribution and commands retain the `alexandria` name for
compatibility: `alexandria-mcp`, `alexandria-web`, and `alexandria-ctl`.

## Run locally

```bash
uv run alexandria-mcp
uv run alexandria-web
```

The MCP command defaults to stdio. The web command binds to loopback at
`http://127.0.0.1:8042`. Provider dispatch incurs external spend and requires the
review/confirmation boundary described in the operational docs.

## Documentation

- [Documentation index](docs/README.md)
- [Orchestration and repository boundary](docs/ORCHESTRATION.md)
- [MCP server](docs/MCP-SERVER.md)
- [Commission web surface](docs/COMMISSION-SURFACE.md)
- [Packaging and deployment](docs/PACKAGING.md)
- [Host service registry and recovery](docs/HOST-OPERATIONS.md)

Corpus design and normative artifact contracts remain authoritative in
[Alexandria's docs](https://github.com/dhk/alexandria/tree/main/docs) and
[`schemas/`](https://github.com/dhk/alexandria/tree/main/schemas).

## Repository map

```text
src/alexandria/       orchestration, MCP/web surfaces, control CLI, adapters
deploy/               pack installer, documentation launcher, host registry
templates/mcp-server/ reusable MCP service scaffold
scripts/              validation, packaging, and corpus-derived exports
tests/                executable behavior and corpus compatibility tests
docs/                 authoritative operational documentation
```

## Status

The repository boundary and independent validation paths are in place. The full
provider-backed, deployed, promote-to-corpus, review, and publication journey has
not been verified end to end on an operator host. Unit tests and package checks do
not establish live credentials, provider availability, tunnel configuration,
source rights, or successful publication.

## Sensitive data

Keep credentials, capability URLs, private inputs, local run records, account
identifiers, and user-specific host details out of Git and documentation. Before
promoting output to public Alexandria, follow its source-rights, personal-data,
provenance, and review rules.
