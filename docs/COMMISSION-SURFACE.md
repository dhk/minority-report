# Commission web surface

The local web surface provides input resolution, a priced review gate,
independent provider dispatch, and inspection of the persisted result.

## Run

```bash
export ALEXANDRIA_REPO=/absolute/path/to/alexandria
uv sync --frozen
uv run alexandria-web
```

Open `http://127.0.0.1:8042`. Use `--host` and `--port` only when you understand
the exposure; the default is loopback. `OPENROUTER_API_KEY` is resolved from the
process environment or the local secrets file selected by
`ALEXANDRIA_SECRETS_FILE`.

## Review and result

Review shows resolved inputs, verbatim brief, selected models, live estimate,
hard ceiling, and exact confirmation before spend. The current request remains
synchronous. A provider failure is distinct from graded silence and remains in
the result.

Runs live under `ALEXANDRIA_DATA_DIR` or the platform user-data directory. The
result exposes claim landscape, heatmap document, report, raw outputs, and
provenance. Exported ZIPs include portable readers beside unchanged run artifacts.
They are local records, not automatically public or authoritative Alexandria
artifacts.

## Inputs and limits

The surface accepts pasted Markdown, PDF/HTML/text/Markdown uploads, and supported
GitHub repository, issue, PR, and blob URLs. Resolution is bounded by file, byte,
and extracted-character limits enforced by the code. PDF text extraction does not
perform OCR.

Private or copyrighted inputs, personal data, credentials, and run records must
remain local unless an authorized review deliberately promotes a safe subset under
Alexandria's governance rules.
