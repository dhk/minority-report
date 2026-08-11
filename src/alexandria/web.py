"""Local single-operator web surface for commissioning Alexandria research."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import uvicorn
from markdown_it import MarkdownIt
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from alexandria.commission import (
    DEFAULT_GRADING_MODEL,
    DEFAULT_MODELS,
    CommissionError,
    CommissionService,
    OpenRouterGateway,
    RunStore,
)
from alexandria.commission_models import Brief, Draft, InputArtifact, RunRecord
from alexandria.flow import build_flow_document, flow_document_json
from alexandria.flow_view import render_flow_page
from alexandria.infrastructure.config import (
    Config,
    HostEnvironmentError,
    RepoNotFoundError,
    load_config,
)
from alexandria.infrastructure.research_repo import list_investigations
from alexandria.infrastructure.secrets import SecretNotFoundError, openrouter_api_key
from alexandria.input_resolution import (
    GitHubResolver,
    InputResolutionError,
    extract_input,
    pasted_input,
    validate_input_set,
)
from alexandria.version import service_version

_MARKDOWN = MarkdownIt(
    "commonmark",
    {"html": False, "linkify": False, "typographer": False},
).enable("table")
_RESULT_TABS = (
    ("landscape", "Claim landscape"),
    ("heatmap", "Heatmap document"),
    ("report", "Report"),
    ("raw", "Raw outputs"),
    ("provenance", "Provenance"),
)
_GROUPS = (
    ("consensus", "Consensus"),
    ("disagreement", "Disagreement"),
    ("novel", "Novel"),
    ("thin", "Thin coverage"),
    ("silent", "Unaddressed"),
)
_BUNDLE_RUNNER = '''#!/usr/bin/env python3
"""Open this Alexandria report bundle in the default browser."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Open the bundled Alexandria report.")
    parser.add_argument("--heatmap", action="store_true", help="open the claim heatmap document")
    parser.add_argument("--no-browser", action="store_true", help="print the selected document URI only")
    args = parser.parse_args()
    report = Path(__file__).resolve().with_name("heatmap.html" if args.heatmap else "report.html")
    if not report.is_file():
        print(f"Document not found: {report}", file=sys.stderr)
        return 1
    uri = report.as_uri()
    print(f"Document: {uri}")
    if not args.no_browser and not webbrowser.open(uri):
        print("No browser opened; paste the URI above into a browser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


ACTIVE_STATUSES = ("draft", "running")

TABS = (
    ("/", "Published work"),
    ("/commission", "New commission"),
    ("/active", "What's active"),
)


def _tabstrip(current: str) -> str:
    """The three top-level surfaces, as links rather than client-side state.

    Links so a tab can be shared and bookmarked, and so this matches how the
    per-run sub-view tabs already work rather than introducing a second
    navigation idiom.
    """
    items = "".join(
        f'<a class="tab{" active" if href == current else ""}" href="{href}">{_e(label)}</a>'
        for href, label in TABS
    )
    return f'<nav class="tabs">{items}</nav>'


def _layout(body: str, *, title: str = "Alexandria", tab: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{_e(title)}</title>
<link rel="stylesheet" href="/assets/tokens/fonts.css"><link rel="stylesheet" href="/assets/tokens/colors.css">
<link rel="stylesheet" href="/assets/tokens/typography.css"><link rel="stylesheet" href="/assets/tokens/spacing.css">
<link rel="stylesheet" href="/assets/tokens/layout.css"><link rel="stylesheet" href="/assets/tokens/base.css">
<link rel="stylesheet" href="/assets/styles.css">
<style>
body{{background:var(--bg);color:var(--text);font-family:system-ui;margin:0}} header{{display:flex;justify-content:space-between;padding:18px 32px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:30}} main{{max-width:1440px;margin:auto;padding:40px 32px 140px}} a{{color:var(--accent)}} .mono{{font-family:var(--font-mono);text-transform:uppercase;letter-spacing:.08em;font-size:11px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:56px}} .card{{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:22px}} label{{display:block;font-weight:600;margin-top:18px}} textarea,input{{box-sizing:border-box;width:100%;margin-top:7px;padding:11px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text)}} textarea{{min-height:92px}} button,.button{{display:inline-block;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:white;padding:12px 18px;text-decoration:none;font:inherit;margin-top:18px}} button[disabled]{{opacity:.45}} .ghost{{background:transparent;color:var(--text)}} .warning{{border-left:3px solid var(--accent-orange);padding-left:12px}} table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;border-bottom:1px solid var(--border);padding:12px 8px;vertical-align:top}} .score{{font-family:var(--font-mono);text-align:center;border-radius:4px}} .score.support-1{{background:rgba(43,80,232,.07)}} .score.support-2{{background:rgba(43,80,232,.13)}} .score.support-3{{background:rgba(43,80,232,.20)}} .score.dispute-1{{background:rgba(224,92,42,.07)}} .score.dispute-2{{background:rgba(224,92,42,.13)}} .score.dispute-3{{background:rgba(224,92,42,.20)}} .failed{{color:var(--accent-orange)}} .silent{{color:var(--text-dim)}} .pill{{display:inline-block;background:var(--bg3);border-radius:4px;padding:4px 7px}} details{{margin:12px 0}} pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg2);padding:16px;border:1px solid var(--border)}}
.result-header{{display:flex;align-items:flex-start;justify-content:space-between;gap:40px;flex-wrap:wrap}} .result-copy{{max-width:62ch}} .result-copy h1{{font-size:34px;line-height:1.15;letter-spacing:-.02em;margin:10px 0 12px}} .result-copy p{{font-size:15.5px;line-height:1.6;color:var(--text-muted);margin:0}} .stats{{display:grid;grid-template-columns:repeat(4,auto);gap:28px}} .stat{{display:flex;flex-direction:column;gap:4px}} .stat strong{{font-size:26px;letter-spacing:-.02em}} .stat span{{color:var(--text-dim)}} .stat.consensus strong{{color:var(--accent)}} .stat.disagreement strong{{color:var(--accent-orange)}} .stat.novel strong{{color:var(--accent-purple)}}
.failure-banner{{border-left:2px solid var(--accent-orange);padding:6px 0 6px 14px;margin-top:26px;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}} .tabs{{display:flex;gap:26px;border-bottom:1px solid var(--border);margin-top:34px}} .tab{{font-family:var(--font-mono);font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;padding:0 0 12px;text-decoration:none;color:var(--text-dim);border-bottom:2px solid transparent;margin-bottom:-1px}} .tab.active{{color:var(--accent);border-bottom-color:var(--accent)}} .filters{{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0 18px}} .filter{{font-family:var(--font-mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;padding:7px 13px;border-radius:4px;border:1px solid var(--border);text-decoration:none;color:var(--text-dim)}} .filter.active{{border-color:var(--accent);background:var(--bg3);color:var(--accent)}} .group-consensus{{color:var(--accent)}} .group-disagreement{{color:var(--accent-orange)}} .group-novel{{color:var(--accent-purple)}} .group-thin{{color:var(--accent-teal)}} .group-silent{{color:var(--text-dim)}} .legend{{display:flex;gap:26px;flex-wrap:wrap;margin-top:30px;color:var(--text-dim)}}
.report-layout{{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:48px;margin-top:30px;align-items:start}} .report-content{{max-width:74ch;font-size:15.5px;line-height:1.75;color:var(--text-muted);overflow-wrap:anywhere}} .report-content h1,.report-content h2,.report-content h3,.report-content h4{{color:var(--text);line-height:1.3;letter-spacing:-.01em;margin:30px 0 8px}} .report-content h1{{font-size:26px}} .report-content h2{{font-size:22px}} .report-content h3{{font-size:20px}} .report-content p,.report-content ul,.report-content ol,.report-content blockquote{{margin:0 0 20px}} .report-content blockquote{{border-left:2px solid var(--accent);padding-left:14px}} .report-content table{{display:block;overflow-x:auto;margin:20px 0}} .report-content th{{color:var(--text)}} .report-content code{{font-family:var(--font-mono);font-size:.9em}} .artifact-card{{position:sticky;top:96px}} .artifact-card p{{font-size:13.5px;line-height:1.6;color:var(--text-muted)}} .artifact-actions{{display:flex;flex-direction:column;gap:8px}} .artifact-action{{box-sizing:border-box;width:100%;margin:0;background:transparent;color:var(--text-muted);border-color:var(--border);font-family:var(--font-mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;text-align:center;cursor:pointer}} .artifact-action:hover{{border-color:var(--accent);color:var(--text)}}
.heatmap-intro{{max-width:72ch;color:var(--text-muted);line-height:1.65;margin:24px 0 30px}} .claim-document{{display:grid;gap:16px}} .claim-block{{border:1px solid var(--border);border-left:5px solid var(--text-dim);border-radius:4px;padding:20px;background:var(--bg2)}} .claim-block.group-consensus{{border-left-color:var(--accent);background:rgba(43,80,232,.06)}} .claim-block.group-disagreement{{border-left-color:var(--accent-orange);background:rgba(224,92,42,.07)}} .claim-block.group-novel{{border-left-color:var(--accent-purple);background:rgba(122,75,190,.07)}} .claim-block.group-thin{{border-left-color:var(--accent-teal);background:rgba(31,139,134,.07)}} .claim-block.group-silent{{border-left-color:var(--text-dim)}} .claim-block h2{{font-size:19px;line-height:1.45;margin:8px 0 18px}} .claim-models{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}} .claim-model{{border:1px solid var(--border);background:var(--bg);padding:10px;border-radius:4px}} .claim-model .score{{display:inline-block;min-width:28px;padding:4px 7px;margin-right:8px}} .claim-model-name{{font-family:var(--font-mono);font-size:11px;color:var(--text-dim);overflow-wrap:anywhere}} .claim-evidence{{font-size:13px;line-height:1.5;color:var(--text-muted);margin:8px 0 0}}
.raw-list,.provenance-layout{{margin-top:30px}} .raw-call{{border-bottom:1px solid var(--border);padding:16px 0}} .raw-call summary{{display:grid;grid-template-columns:minmax(180px,240px) 1fr auto;gap:20px;align-items:baseline;cursor:pointer}} .raw-call-name{{display:flex;flex-direction:column;gap:3px}} .raw-meta{{font-family:var(--font-mono);font-size:11.5px;color:var(--text-dim)}} .provenance-layout{{display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:start}} .section-rule{{display:flex;align-items:center;gap:20px;padding-bottom:16px}} .section-rule span:last-child{{flex:1;height:1px;background:var(--border)}} .manifest-row{{display:grid;grid-template-columns:190px 1fr;gap:16px;padding:9px 0;border-bottom:1px solid var(--border)}} .manifest-row dd,.manifest-row dt{{margin:0}} .manifest-row dd{{font-family:var(--font-mono);font-size:12px;color:var(--text-muted);word-break:break-word}} .limitation{{border-left:2px solid var(--border);padding:4px 0 4px 12px;margin-bottom:16px;font-size:14.5px;line-height:1.6}} .empty-state{{border-left:2px solid var(--border);padding-left:14px;color:var(--text-muted)}}
@media(max-width:900px){{.grid,.report-layout,.provenance-layout{{grid-template-columns:1fr}} .stats{{grid-template-columns:repeat(2,auto)}} .artifact-card{{position:static}} .raw-call summary{{grid-template-columns:1fr}}}} @media(max-width:600px){{main{{padding:28px 18px 100px}} .tabs{{gap:16px;overflow-x:auto}}}}
</style></head><body><header><div><strong>Alexandria</strong> <span class="mono">Research commissions</span></div><nav><a href="/">Published work</a></nav></header><main>{_tabstrip(tab) if tab else ""}{body}</main><footer style="max-width:1440px;margin:auto;padding:28px 32px 48px;border-top:1px solid var(--border);color:var(--text-dim);font-size:13px"><a href="https://www.dhk.io">www.dhk.io</a> &middot; tool: <a href="https://github.com/dhk/minority-report">dhk/minority-report</a> &middot; corpus: <a href="https://github.com/dhk/alexandria">dhk/alexandria</a></footer></body></html>"""


def _input_rows(inputs: Sequence[InputArtifact]) -> str:
    rows = []
    for item in inputs:
        warning = f'<div class="warning">{_e(item.warning)}</div>' if item.warning else ""
        rows.append(
            f"<tr><td>{_e(item.name)}{warning}</td><td>{_e(item.state)}</td>"
            f'<td class="mono">{item.bytes} bytes · {item.extracted_chars} chars · {_e(item.sha256[:12])}</td></tr>'
        )
    return "".join(rows)


def _investigations(config: Config) -> str:
    """dhk/alexandria#34's flow view (GET /flow/{slug}) had no path to it from
    anywhere in this app -- only reachable if you already knew the URL.
    This is that path: every research/ investigation, linked to its flow.
    """
    investigations = list_investigations(config)
    if not investigations:
        return "<p>No research investigations yet.</p>"
    rows = "".join(
        f'<tr><td><a href="/flow/{_e(inv.slug)}">{_e(inv.title or inv.slug)}</a></td>'
        f"<td>{_e(inv.assurance_level or 'unset')}</td>"
        f"<td>{_e(inv.current_stage or 'no lifecycle dirs yet')}</td></tr>"
        for inv in investigations
    )
    return (
        "<table><thead><tr><th>Investigation</th><th>Assurance</th><th>Stage</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _history(store: RunStore, *, statuses: tuple[str, ...] | None = None, empty: str = "") -> str:
    """Run rows, optionally narrowed to some statuses.

    "What's active" is a filter over the same history rather than a second
    notion of state: RunStatus already distinguishes draft/running from the
    finished ones.
    """
    runs = [run for run in store.list_runs() if statuses is None or run.status in statuses]
    if not runs:
        return f"<p>{empty or 'No commissioned runs yet.'}</p>"
    rows = "".join(
        f'<tr><td><a href="/runs/{_e(run.run_id)}">{_e(run.run_id)}</a></td>'
        f"<td>{_e(run.status)}</td><td>{len(run.inputs)}</td><td>{len(run.dispatched_models)}</td>"
        f"<td>{'$' + format(run.cost_actual, '.4f') if run.cost_actual is not None else '—'}</td></tr>"
        for run in runs
    )
    return f"<table><thead><tr><th>Run</th><th>Status</th><th>Inputs</th><th>Models</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table>"


def _commission_form(models: str) -> str:
    return f"""<section class="card"><form action="/review" method="post" enctype="multipart/form-data">
<div class="grid"><div><h2>Inputs</h2>
<label>Paste content<textarea name="pasted_content" placeholder="Paste source material verbatim"></textarea></label>
<label>GitHub URL<input name="repository_url" type="url" placeholder="Repo, issue, PR, or blob URL"></label>
<label>Files<input name="files" type="file" multiple accept=".pdf,.html,.htm,.txt,.md,.markdown"></label>
<p class="mono">8 files · 20 MB · 400k extracted characters</p></div>
<div><h2>Research brief</h2>
<label>Task<textarea name="task" required></textarea></label><label>Context<textarea name="context"></textarea></label>
<label>Constraints<textarea name="constraints"></textarea></label><label>Output needs<textarea name="output_needs"></textarea></label>
<label>Research models<textarea name="models">{_e(models)}</textarea></label>
<label>Grading model<input name="grading_model" value="{_e(DEFAULT_GRADING_MODEL)}"></label>
<label>Hard ceiling (USD)<input name="ceiling_usd" type="number" min="0.01" step="0.01" value="1.00"></label>
<button type="submit">Review commission →</button></div></div></form></section>"""


async def health(request: Request) -> Response:
    """Names itself, so a checker verifies identity rather than "something answered".

    checks.py compares this `service` field against the pack declaration; a
    health check that only proves a socket is open would pass against whatever
    else happened to claim the port.
    """
    return JSONResponse({"service": "alexandria-web", "version": service_version()})


async def homepage(request: Request) -> HTMLResponse:
    """Published work: what exists beats what you might make, so this lands."""
    config: Config = request.app.state.config
    body = f"""
<h1>Published work</h1>
<p>Every investigation in the research corpus, and how far each one has come.</p>
{_investigations(config)}
"""
    return HTMLResponse(_layout(body, tab="/"))


async def commission(request: Request) -> HTMLResponse:
    models = "\n".join(DEFAULT_MODELS)
    body = f"""
<h1>Commission the same question to several models.</h1>
<p>Keep what each one said. Agreement is model agreement. It is not verification.</p>
{_commission_form(models)}
"""
    return HTMLResponse(_layout(body, title="New commission · Alexandria", tab="/commission"))


async def active(request: Request) -> HTMLResponse:
    """Runs not yet finished, and every finished one beneath them.

    Completed runs stay reachable here: "what's active" is not "everything",
    and history with nowhere to live is history nobody finds.
    """
    config: Config = request.app.state.config
    store = RunStore(config.data_dir)
    body = f"""
<h1>What's active</h1>
<p>Commissions still drafting or running. A run leaves this list when it finishes.</p>
{_history(store, statuses=ACTIVE_STATUSES, empty="Nothing is running or drafting right now.")}
<h2>Finished runs</h2>
{_history(store, statuses=("completed", "partial", "failed"), empty="No finished runs yet.")}
"""
    return HTMLResponse(_layout(body, title="What's active · Alexandria", tab="/active"))


async def _uploaded_inputs(form: Any) -> list[InputArtifact]:
    inputs: list[InputArtifact] = []
    for upload in form.getlist("files"):
        if not isinstance(upload, UploadFile) or not upload.filename:
            continue
        raw = await upload.read(20 * 1024 * 1024 + 1)
        inputs.append(extract_input(upload.filename, raw))
    return inputs


async def review(request: Request) -> HTMLResponse:
    try:
        form = await request.form(max_files=8, max_fields=30, max_part_size=20 * 1024 * 1024)
        inputs = await _uploaded_inputs(form)
        pasted = pasted_input(str(form.get("pasted_content") or ""))
        if pasted:
            inputs.insert(0, pasted)
        repository_url = str(form.get("repository_url") or "").strip()
        if repository_url:
            async with GitHubResolver() as resolver:
                inputs.extend(await resolver.resolve(repository_url))
        validate_input_set(inputs)
        brief = Brief(
            task=str(form.get("task") or ""),
            context=str(form.get("context") or ""),
            constraints=str(form.get("constraints") or ""),
            output_needs=str(form.get("output_needs") or ""),
        )
        models = [line.strip() for line in str(form.get("models") or "").splitlines()]
        grading_model = str(form.get("grading_model") or DEFAULT_GRADING_MODEL).strip()
        ceiling_value = form.get("ceiling_usd")
        ceiling = float(ceiling_value) if isinstance(ceiling_value, str | int | float) else 1.0
        async with OpenRouterGateway(openrouter_api_key()) as gateway:
            service = CommissionService(request.app.state.config, gateway)
            draft = await service.create_draft(brief, inputs, models, grading_model, ceiling)
    except (CommissionError, InputResolutionError, SecretNotFoundError, ValueError) as exc:
        return HTMLResponse(
            _layout(
                f'<h1>Commission not ready</h1><p class="warning">{_e(exc)}</p><a class="button ghost" href="/">Back</a>'
            ),
            status_code=400,
        )
    return HTMLResponse(_review_page(draft))


def _review_page(draft: Draft) -> str:
    if draft.estimate_usd is None:
        estimate = "Estimate unavailable"
        price_note = "The OpenRouter key limit is the active ceiling. Dispatch remains available."
        blocked = False
        label = "Run research"
    else:
        # Same correction as the MCP review surface (dhk/alexandria#32): this is
        # an estimate, not a cap. Runs have come in at ~2x it.
        estimate = f"${draft.estimate_usd:.4f}"
        blocked = draft.estimate_usd > draft.ceiling_usd
        price_note = (
            f"An estimate, not a cap. Hard ceiling ${draft.ceiling_usd:.2f} "
            "is the only bound enforced."
            if not blocked
            else f"Estimate exceeds the ${draft.ceiling_usd:.2f} ceiling."
        )
        label = "Over ceiling — dispatch disabled" if blocked else f"Run research · {estimate}"
    disabled = " disabled" if blocked else ""
    models = "".join(f"<li><code>{_e(model)}</code></li>" for model in draft.models)
    body = f"""<div class="mono">02 — Review</div><h1>Review commission</h1>
<div class="grid"><section><h2>What leaves this machine</h2><table><thead><tr><th>Input</th><th>State</th><th>Metadata</th></tr></thead><tbody>{_input_rows(draft.inputs)}</tbody></table>
<h2>Models</h2><ul>{models}</ul><p>Models research independently and never see each other's output.</p>
<details><summary>Brief sent verbatim</summary><pre>{_e(draft.brief.verbatim())}</pre></details></section>
<aside class="card"><div class="mono">Estimated cost</div><h2>{_e(estimate)}</h2><p>{_e(price_note)}</p>
<p>Keys are resolved locally and are not exposed to this interface.</p>
<form action="/dispatch/{_e(draft.draft_id)}" method="post"><button{disabled}>{_e(label)}</button></form></aside></div>"""
    return _layout(body, title="Review commission · Alexandria")


async def dispatch(request: Request) -> Response:
    draft_id = request.path_params["draft_id"]
    try:
        async with OpenRouterGateway(openrouter_api_key()) as gateway:
            run = await CommissionService(request.app.state.config, gateway).dispatch(draft_id)
    except (CommissionError, SecretNotFoundError) as exc:
        return HTMLResponse(
            _layout(f'<h1>Run did not start</h1><p class="warning">{_e(exc)}</p>'), status_code=400
        )
    return RedirectResponse(f"/runs/{run.run_id}", status_code=303)


def _load_result(
    config: Config, run: RunRecord
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    run_dir = RunStore(config.data_dir).run_dir(run.run_id)
    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    with (run_dir / "scores.csv").open(encoding="utf-8", newline="") as stream:
        scores = list(csv.DictReader(stream))
    return claims, scores


def _load_raw_calls(run_dir: Path) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for path in sorted((run_dir / "raw").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            call = cast(dict[str, Any], payload)
            call["artifact_name"] = path.name
            calls.append(call)
    return calls


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else {}


def _brief_question(run_dir: Path, run: RunRecord) -> str:
    path = run_dir / "brief.md"
    if not path.is_file():
        return f"Research run {run.run_id}"
    brief = path.read_text(encoding="utf-8")
    if brief.startswith("Task\n"):
        question = brief.removeprefix("Task\n").split("\n\nContext\n", maxsplit=1)[0].strip()
        if question:
            return question
    return f"Research run {run.run_id}"


def _display_headline(question: str, *, limit: int = 112) -> str:
    compact = " ".join(question.split())
    if len(compact) <= limit:
        return compact
    shortened = compact[: limit + 1].rsplit(" ", maxsplit=1)[0]
    return f"{shortened}…"


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "unavailable"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes}:{remainder:02d}"


def _completion_qualifier(run: RunRecord, failed_calls: int) -> str:
    if failed_calls:
        noun = "call" if failed_calls == 1 else "calls"
        return f"{run.status} with {failed_calls} failed {noun}"
    return run.status


def _tab_navigation(run_id: str, active: str) -> str:
    return (
        '<nav class="tabs" aria-label="Result views">'
        + "".join(
            (
                f'<a class="tab{" active" if key == active else ""}" '
                f'href="/runs/{_e(run_id)}?tab={key}"'
                f"{' aria-current="page"' if key == active else ''}>{_e(label)}</a>"
            )
            for key, label in _RESULT_TABS
        )
        + "</nav>"
    )


def _score_presentation(row: dict[str, str] | None) -> tuple[str, str, str]:
    raw_score = row.get("score") if row else None
    if raw_score in {None, ""}:
        return "✕", "failed", "Call failed; no output exists to grade."
    assert row is not None
    score = int(raw_score)
    if score == 0:
        return "—", "silent", "Model responded; no bearing statement found."
    css = f"{'support' if score > 0 else 'dispute'}-{abs(score)}"
    return f"{score:+d}", css, row.get("quote") or "Evidence quote unavailable."


def _landscape_view(
    run: RunRecord,
    claims: list[dict[str, Any]],
    scores: list[dict[str, str]],
    selected_group: str,
) -> str:
    score_map = {(row["claim_id"], row["model_id"]): row for row in scores}
    counts = {key: sum(claim.get("group") == key for claim in claims) for key, _ in _GROUPS}
    filters = [("all", "All", len(claims)), *[(key, label, counts[key]) for key, label in _GROUPS]]
    filter_links = "".join(
        f'<a class="filter{" active" if key == selected_group else ""}" '
        f'href="/runs/{_e(run.run_id)}?tab=landscape&amp;group={key}">{_e(label)} · {count}</a>'
        for key, label, count in filters
    )
    shown_claims = (
        claims
        if selected_group == "all"
        else [claim for claim in claims if claim.get("group") == selected_group]
    )
    headers = "".join(f"<th>{_e(model)}</th>" for model in run.dispatched_models)
    rows = []
    for claim in shown_claims:
        cells = []
        claim_id = str(claim.get("claim_id") or "")
        for model in run.dispatched_models:
            row = score_map.get((claim_id, model))
            value, css, title = _score_presentation(row)
            cells.append(
                f'<td class="score {css}" title="{_e(title)}" aria-label="{_e(title)}">'
                f"{_e(value)}</td>"
            )
        group = str(claim.get("group") or "silent")
        group_label = dict(_GROUPS).get(group, group)
        responding = claim.get("responding_model_count", "unavailable")
        rows.append(
            f"<tr><td>{_e(claim.get('text') or 'Claim text unavailable.')}<br>"
            f'<span class="pill mono group-{_e(group)}">{_e(group_label)} · '
            f"{_e(responding)}/{len(run.dispatched_models)}</span></td>{''.join(cells)}</tr>"
        )
    table_rows = "".join(rows) or '<tr><td colspan="99">No claims match this filter.</td></tr>'
    return f"""
<div class="filters">{filter_links}</div>
<div class="landscape-table"><table><thead><tr><th>Canonical claim</th>{headers}</tr></thead>
<tbody>{table_rows}</tbody></table></div>
<div class="legend mono"><span>+3 / +2 / +1 supports, by strength</span>
<span>−1 / −2 / −3 contradicts</span><span>— no bearing statement found</span>
<span>✕ call failed, not silence</span></div>"""


def _heatmap_claim_blocks(
    run: RunRecord,
    claims: list[dict[str, Any]],
    scores: list[dict[str, str]],
) -> str:
    score_map = {(row["claim_id"], row["model_id"]): row for row in scores}
    blocks = []
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        group = str(claim.get("group") or "silent")
        group_label = dict(_GROUPS).get(group, group)
        responding = claim.get("responding_model_count", "unavailable")
        models = []
        for model in run.dispatched_models:
            row = score_map.get((claim_id, model))
            value, css, evidence = _score_presentation(row)
            models.append(
                f'<div class="claim-model"><span class="score {css}" title="{_e(evidence)}">'
                f'{_e(value)}</span><span class="claim-model-name">{_e(model)}</span>'
                f'<p class="claim-evidence">{_e(evidence)}</p></div>'
            )
        blocks.append(
            f'<article class="claim-block group-{_e(group)}">'
            f'<div class="mono group-{_e(group)}">{_e(group_label)} · '
            f"{_e(responding)}/{len(run.dispatched_models)} responding · {_e(claim_id)}</div>"
            f"<h2>{_e(claim.get('text') or 'Claim text unavailable.')}</h2>"
            f'<div class="claim-models">{"".join(models)}</div></article>'
        )
    return "".join(blocks) or '<p class="empty-state">No canonical claims are available.</p>'


def _heatmap_view(
    run: RunRecord,
    claims: list[dict[str, Any]],
    scores: list[dict[str, str]],
) -> str:
    return f"""
<p class="heatmap-intro">This deterministic, claim-first document color-codes each
canonical claim by its relationship across the responding models. Color describes
model agreement, disagreement, novelty, coverage, or silence—not factual truth.
Alexandria does not yet record exact claim spans in the synthesized prose, so this
view does not pretend to highlight words that cannot be mapped to evidence.</p>
<div class="legend mono"><span class="group-consensus">Consensus</span>
<span class="group-disagreement">Disagreement</span><span class="group-novel">Novel</span>
<span class="group-thin">Thin coverage</span><span class="group-silent">Unaddressed</span></div>
<section class="claim-document">{_heatmap_claim_blocks(run, claims, scores)}</section>
<div class="artifact-actions" style="max-width:320px;margin-top:22px">
<a class="button artifact-action" href="/runs/{_e(run.run_id)}/heatmap.html">Open standalone heatmap</a>
<a class="button artifact-action" href="/runs/{_e(run.run_id)}/bundle.zip">Download bundle</a>
</div>"""


def _report_view(run: RunRecord, run_dir: Path) -> str:
    report_path = run_dir / "report.md"
    if report_path.is_file():
        markdown = report_path.read_text(encoding="utf-8")
        rendered = _MARKDOWN.render(markdown)
    else:
        rendered = (
            '<p class="empty-state">Report unavailable. This run has no report.md artifact.</p>'
        )
    report_url = f"/runs/{_e(run.run_id)}/report.md"
    bundle_url = f"/runs/{_e(run.run_id)}/bundle.zip"
    return f"""
<div class="report-layout"><article class="report-content">{rendered}</article>
<aside class="card artifact-card"><div class="mono">Artifact</div>
<p>Deterministic Markdown generated from the graded claims. It is a reading surface, not a higher authority than the evidence. The bundle includes a standalone HTML reader.</p>
<div class="artifact-actions">
<button class="artifact-action" type="button" data-copy-markdown="{report_url}">Copy Markdown</button>
<a class="button artifact-action" href="{bundle_url}">Download bundle</a>
</div></aside></div>
<script>
document.querySelectorAll('[data-copy-markdown]').forEach((button) => {{
  button.addEventListener('click', async () => {{
    const response = await fetch(button.dataset.copyMarkdown);
    if (!response.ok) {{ button.textContent = 'Report unavailable'; return; }}
    await navigator.clipboard.writeText(await response.text());
    button.textContent = 'Copied';
  }});
}});
</script>"""


def _call_metadata(call: dict[str, Any]) -> str:
    status = str(call.get("status") or "status unavailable")
    prompt = call.get("prompt_tokens")
    completion = call.get("completion_tokens")
    token_text = (
        f"{prompt} in / {completion} out"
        if prompt is not None and completion is not None
        else "tokens unavailable"
    )
    cost = call.get("cost")
    cost_text = f"${float(cost):.4f}" if isinstance(cost, int | float) else "cost unavailable"
    latency = call.get("latency_ms")
    latency_text = f"{latency} ms" if latency is not None else "latency unavailable"
    return f"{status} · {token_text} · {cost_text} · {latency_text}"


def _raw_view(calls: list[dict[str, Any]]) -> str:
    rows = []
    for call in calls:
        model = str(call.get("model_id") or call.get("artifact_name") or "Unknown call")
        resolved = str(call.get("resolved_model_id") or "resolved model unavailable")
        body = call.get("raw_response") or call.get("error") or "No response body was preserved."
        rows.append(
            f'<details class="raw-call"><summary><span class="raw-call-name"><strong>{_e(model)}</strong>'
            f'<span class="raw-meta">{_e(resolved)}</span></span><span class="raw-meta">'
            f'{_e(_call_metadata(call))}</span><span class="mono">Open raw</span></summary>'
            f"<pre>{_e(body)}</pre></details>"
        )
    return (
        '<div class="raw-list">'
        + ("".join(rows) or '<p class="empty-state">No raw call artifacts were preserved.</p>')
        + "</div>"
    )


def _provenance_view(run: RunRecord, manifest: dict[str, Any]) -> str:
    rows = []
    for key, value in manifest.items():
        shown = value if isinstance(value, str | int | float) else json.dumps(value, sort_keys=True)
        rows.append(
            f'<div class="manifest-row"><dt class="mono">{_e(key.replace("_", " "))}</dt>'
            f"<dd>{_e(shown)}</dd></div>"
        )
    manifest_rows = "".join(rows) or (
        '<p class="empty-state">Manifest unavailable. This run has no manifest.json artifact.</p>'
    )
    limitations = list(run.limitations)
    limitations.append(
        "Agreement is model agreement. It describes how models related to a claim; "
        "it does not verify that claim."
    )
    limitation_rows = "".join(f'<div class="limitation">{_e(item)}</div>' for item in limitations)
    return f"""
<div class="provenance-layout"><section><div class="section-rule"><span class="mono">Manifest</span><span></span></div>
<dl>{manifest_rows}</dl></section><section><div class="section-rule"><span class="mono">Limitations</span><span></span></div>
{limitation_rows}<div class="artifact-actions"><a class="button artifact-action" href="/runs/{_e(run.run_id)}/bundle.zip">Download artifacts</a></div>
</section></div>"""


async def result(request: Request) -> HTMLResponse:
    config: Config = request.app.state.config
    try:
        store = RunStore(config.data_dir)
        run = store.load_run(request.path_params["run_id"])
        claims, scores = _load_result(config, run)
        run_dir = store.run_dir(run.run_id)
        calls = _load_raw_calls(run_dir)
        manifest = _load_manifest(run_dir)
    except (CommissionError, OSError, ValueError) as exc:
        return HTMLResponse(_layout(f"<h1>Run unavailable</h1><p>{_e(exc)}</p>"), status_code=404)
    active_tab = request.query_params.get("tab", "landscape")
    if active_tab not in {key for key, _ in _RESULT_TABS}:
        active_tab = "landscape"
    selected_group = request.query_params.get("group", "all")
    if selected_group not in {"all", *(key for key, _ in _GROUPS)}:
        selected_group = "all"
    research_calls = [
        call for call in calls if str(call.get("model_id") or "") in run.dispatched_models
    ]
    failed_calls = sum(call.get("status") == "failed" for call in research_calls)
    responded = len(run.dispatched_models) - failed_calls
    group_counts = {key: sum(claim.get("group") == key for claim in claims) for key, _ in _GROUPS}
    cost = f"${run.cost_actual:.4f}" if run.cost_actual is not None else "unavailable"
    question = _brief_question(run_dir, run)
    stats = f"""<div class="stats"><div class="stat consensus"><strong>{group_counts["consensus"]}</strong><span class="mono">Consensus</span></div>
<div class="stat disagreement"><strong>{group_counts["disagreement"]}</strong><span class="mono">Disagreement</span></div>
<div class="stat novel"><strong>{group_counts["novel"]}</strong><span class="mono">Novel</span></div>
<div class="stat"><strong>{_e(cost)} · {_e(_format_elapsed(run.elapsed_seconds))}</strong><span class="mono">Cost · elapsed</span></div></div>"""
    failure_banner = ""
    if failed_calls:
        noun = "call failed" if failed_calls == 1 else "calls failed"
        failure_banner = (
            f'<div class="failure-banner"><strong>{failed_calls} {noun}. Their cells read ✕, '
            f'not silence.</strong><a class="mono" href="/runs/{_e(run.run_id)}?tab=provenance">'
            "See limitations →</a></div>"
        )
    if active_tab == "heatmap":
        active_view = _heatmap_view(run, claims, scores)
    elif active_tab == "report":
        active_view = _report_view(run, run_dir)
    elif active_tab == "raw":
        active_view = _raw_view(calls)
    elif active_tab == "provenance":
        active_view = _provenance_view(run, manifest)
    else:
        active_view = _landscape_view(run, claims, scores, selected_group)
    body = f"""<section class="result-header"><div class="result-copy"><div class="mono">Run {_e(run.run_id)} · brief rev. {_e(run.brief_revision)} · {_e(_completion_qualifier(run, failed_calls))}</div>
<h1 title="{_e(question)}">{_e(_display_headline(question))}</h1><p>{responded} of {len(run.dispatched_models)} models returned research. {len(claims)} canonical claims were extracted from their outputs and graded blind.</p></div>{stats}</section>
{failure_banner}{_tab_navigation(run.run_id, active_tab)}{active_view}
<p class="legend mono">Agreement is model agreement. It is not verification.</p>"""
    return HTMLResponse(_layout(body, title=f"{run.run_id} · Alexandria"))


async def report_markdown(request: Request) -> Response:
    config: Config = request.app.state.config
    try:
        store = RunStore(config.data_dir)
        run = store.load_run(request.path_params["run_id"])
        path = store.run_dir(run.run_id) / "report.md"
        markdown = path.read_text(encoding="utf-8")
    except (CommissionError, OSError, ValueError) as exc:
        return Response(f"Report unavailable: {exc}\n", status_code=404, media_type="text/plain")
    return Response(
        markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'inline; filename="{run.run_id}-report.md"'},
    )


def _standalone_heatmap(
    run: RunRecord,
    claims: list[dict[str, Any]],
    scores: list[dict[str, str]],
) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{_e(run.run_id)} · Alexandria claim heatmap</title>
<style>
:root{{--text:#202124;--muted:#646971;--border:#ccd0d5;--panel:#fff;--blue:#3155c6;
--orange:#d45f2f;--purple:#7b4bbe;--teal:#1f8b86}}*{{box-sizing:border-box}}
body{{margin:0;background:#f7f7f4;color:var(--text);font:15px/1.6 system-ui}}
main{{max-width:1100px;margin:auto;padding:64px 28px 100px}}header{{border-bottom:1px solid var(--border);padding-bottom:24px;margin-bottom:32px}}
.mono,.claim-model-name{{font:11px/1.5 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase}}
h1{{font-size:38px;line-height:1.2;margin:10px 0}}.intro{{max-width:76ch;color:var(--muted)}}
.legend{{display:flex;gap:20px;flex-wrap:wrap;margin:26px 0}}.group-consensus{{color:var(--blue)}}
.group-disagreement{{color:var(--orange)}}.group-novel{{color:var(--purple)}}.group-thin{{color:var(--teal)}}
.group-silent{{color:var(--muted)}}.claim-document{{display:grid;gap:16px}}
.claim-block{{border:1px solid var(--border);border-left:5px solid var(--muted);border-radius:4px;padding:20px;background:var(--panel)}}
.claim-block.group-consensus{{border-left-color:var(--blue);background:#f1f4ff}}
.claim-block.group-disagreement{{border-left-color:var(--orange);background:#fff3ee}}
.claim-block.group-novel{{border-left-color:var(--purple);background:#f7f1ff}}
.claim-block.group-thin{{border-left-color:var(--teal);background:#eef9f7}}
.claim-block h2{{font-size:19px;line-height:1.45;margin:8px 0 18px}}.claim-models{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}
.claim-model{{border:1px solid var(--border);background:var(--panel);padding:10px;border-radius:4px}}
.score{{display:inline-block;min-width:32px;padding:4px 7px;margin-right:8px;border-radius:4px;text-align:center;font-family:ui-monospace,monospace}}
.score.support-1{{background:#e8edff}}.score.support-2{{background:#cfdaff}}.score.support-3{{background:#b6c7ff}}
.score.dispute-1{{background:#ffebe3}}.score.dispute-2{{background:#ffd8c8}}.score.dispute-3{{background:#ffc4ad}}
.failed{{color:var(--orange)}}.silent{{color:var(--muted)}}.claim-model-name{{color:var(--muted);overflow-wrap:anywhere}}
.claim-evidence{{font-size:13px;line-height:1.5;color:var(--muted);margin:8px 0 0}}
@media(prefers-color-scheme:dark){{:root{{--text:#eee;--muted:#aaa;--border:#3b3e43;--panel:#202225}}
body{{background:#17181a}}.claim-block.group-consensus{{background:#1e263f}}.claim-block.group-disagreement{{background:#35231e}}
.claim-block.group-novel{{background:#2b2338}}.claim-block.group-thin{{background:#1c302e}}
.score.support-1,.score.support-2,.score.support-3{{background:#27345e}}.score.dispute-1,.score.dispute-2,.score.dispute-3{{background:#593025}}}}
</style></head><body><main><header><div class="mono">Alexandria · {_e(run.run_id)} · brief rev. {_e(run.brief_revision)}</div>
<h1>Claim heatmap</h1><p class="intro">A deterministic, claim-first view of the grading
matrix. Color describes the relationship among model responses, not factual truth.
The artifacts do not record exact claim spans in report prose, so this document does
not imply a word-level mapping.</p></header>
<div class="legend mono"><span class="group-consensus">Consensus</span>
<span class="group-disagreement">Disagreement</span><span class="group-novel">Novel</span>
<span class="group-thin">Thin coverage</span><span class="group-silent">Unaddressed</span>
<span>— responded, no bearing</span><span>✕ call failed</span></div>
<section class="claim-document">{_heatmap_claim_blocks(run, claims, scores)}</section>
</main></body></html>"""


async def heatmap_html(request: Request) -> Response:
    config: Config = request.app.state.config
    try:
        store = RunStore(config.data_dir)
        run = store.load_run(request.path_params["run_id"])
        claims, scores = _load_result(config, run)
    except (CommissionError, OSError, ValueError) as exc:
        return Response(f"Heatmap unavailable: {exc}\n", status_code=404, media_type="text/plain")
    # The bundled copy of this document has to work as a file with no server,
    # so _standalone_heatmap stays standalone. The way back is added to the
    # served response only (#35).
    document = _standalone_heatmap(run, claims, scores)
    banner = (
        f'<p style="font:13px system-ui;padding:14px 20px;margin:0;'
        f'border-bottom:1px solid #ccc">'
        f'<a href="/runs/{_e(run.run_id)}">&larr; Back to run {_e(run.run_id)}</a></p>'
    )
    served = (
        document.replace("<body>", f"<body>{banner}", 1)
        if "<body>" in document
        else banner + document
    )
    return Response(
        served,
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{run.run_id}-heatmap.html"'},
    )


def _standalone_report(run: RunRecord, markdown: str) -> str:
    rendered = _MARKDOWN.render(markdown)
    cost = f"${run.cost_actual:.4f}" if run.cost_actual is not None else "unavailable"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{_e(run.run_id)} · Alexandria report</title>
<style>
:root{{color-scheme:light dark}}body{{margin:0;background:#f7f7f4;color:#202124;font:16px/1.72 system-ui}}
main{{max-width:74ch;margin:auto;padding:64px 28px 100px}}header{{border-bottom:1px solid #ccd0d5;padding-bottom:24px;margin-bottom:36px}}
.meta{{font:12px/1.5 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;color:#6a6e76}}
h1,h2,h3,h4{{line-height:1.25;letter-spacing:-.015em}}h1{{font-size:38px}}h2{{margin-top:42px}}a{{color:#3155c6}}
blockquote{{border-left:3px solid #3155c6;margin-left:0;padding-left:18px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%;display:block;overflow-x:auto}}th,td{{border-bottom:1px solid #ccd0d5;padding:10px;text-align:left}}
.notice{{border-left:3px solid #d56a35;padding-left:14px;margin-top:24px;color:#5d6067}}
@media(prefers-color-scheme:dark){{body{{background:#17181a;color:#eee}}.meta,.notice{{color:#aaa}}header,th,td{{border-color:#3b3e43}}}}
</style></head><body><main><header><div class="meta">Alexandria · {_e(run.run_id)} · brief rev. {_e(run.brief_revision)}</div>
<h1>Research report</h1><div class="meta">Status {_e(run.status)} · actual cost {_e(cost)}</div>
<p class="notice">Agreement is model agreement. It is not verification. The original evidence and grading artifacts are included beside this reader.</p>
</header><article>{rendered}</article></main></body></html>"""


def _bundle_readme(run: RunRecord) -> str:
    return f"""ALEXANDRIA RESEARCH REPORT — {run.run_id}

READ THE OUTPUTS

Narrative report:
  Open report.html, or run: python3 open-report.py

Color-coded claim heatmap:
  Open heatmap.html, or run: python3 open-report.py --heatmap

On a headless machine, add `--no-browser` and open the printed file URI on a
machine that has access to this unzipped directory.

The original report.md, claims, scores, manifest, raw model outputs, brief, and
inputs are preserved alongside the readers. The HTML files are only reading
surfaces. Agreement is model agreement; it is not verification.
"""


def _write_bundle_text(
    bundle: zipfile.ZipFile,
    run: RunRecord,
    relative: str,
    content: str,
    *,
    mode: int = 0o644,
) -> None:
    created = run.created_at
    info = zipfile.ZipInfo(
        filename=f"{run.run_id}/{relative}",
        date_time=(
            created.year,
            created.month,
            created.day,
            created.hour,
            created.minute,
            created.second,
        ),
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = mode << 16
    bundle.writestr(info, content.encode("utf-8"))


async def artifact_bundle(request: Request) -> Response:
    config: Config = request.app.state.config
    try:
        store = RunStore(config.data_dir)
        run = store.load_run(request.path_params["run_id"])
        run_dir = store.run_dir(run.run_id)
        claims, scores = _load_result(config, run)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(run_dir.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    bundle.write(path, arcname=str(Path(run.run_id) / path.relative_to(run_dir)))
            report_path = run_dir / "report.md"
            report_source = (
                report_path.read_text(encoding="utf-8")
                if report_path.is_file()
                else "# Report unavailable\n"
            )
            _write_bundle_text(bundle, run, "report.html", _standalone_report(run, report_source))
            _write_bundle_text(
                bundle,
                run,
                "heatmap.html",
                _standalone_heatmap(run, claims, scores),
            )
            _write_bundle_text(bundle, run, "open-report.py", _BUNDLE_RUNNER, mode=0o755)
            _write_bundle_text(bundle, run, "HOW-TO-READ.txt", _bundle_readme(run))
    except (CommissionError, OSError, ValueError, zipfile.BadZipFile) as exc:
        return Response(f"Bundle unavailable: {exc}\n", status_code=404, media_type="text/plain")
    return Response(
        archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{run.run_id}-artifacts.zip"'},
    )


async def flow(request: Request) -> Response:
    """RFC-0007: the idea-to-expression flow for one research/ investigation."""
    config: Config = request.app.state.config
    slug = request.path_params["slug"]
    document = build_flow_document(config, slug)
    if document is None:
        return HTMLResponse(
            _layout(f"<h1>No investigation named &ldquo;{_e(slug)}&rdquo;</h1>"), status_code=404
        )
    return HTMLResponse(render_flow_page(flow_document_json(document)))


def create_app(config: Config | None = None) -> Starlette:
    config = config or load_config()
    routes: list[Any] = [
        Route("/health", health),
        Route("/", homepage),
        Route("/commission", commission),
        Route("/active", active),
        Route("/review", review, methods=["POST"]),
        Route("/dispatch/{draft_id}", dispatch, methods=["POST"]),
        Route("/runs/{run_id}", result),
        Route("/runs/{run_id}/report.md", report_markdown),
        Route("/runs/{run_id}/heatmap.html", heatmap_html),
        Route("/runs/{run_id}/bundle.zip", artifact_bundle),
        Route("/flow/{slug}", flow),
    ]
    matches = list((config.repo_root / "docs/ux/prototype/_ds").glob("dhk-design-system-*"))
    if matches:
        routes.append(Mount("/assets", app=StaticFiles(directory=matches[0]), name="assets"))
    app = Starlette(routes=routes)
    app.state.config = config
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria-web")
    # Loopback by default: it always binds, it cannot expose the surface to a
    # network by accident, and the health check can always reach it. Reaching
    # this from another device means either a tunnel or setting
    # ALEXANDRIA_WEB_HOST deliberately -- see docs/SERVER-style notes in #39.
    parser.add_argument("--host", default=os.environ.get("ALEXANDRIA_WEB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("ALEXANDRIA_WEB_PORT", "8798"))
    )
    args = parser.parse_args(argv)
    try:
        app = create_app()
    except (HostEnvironmentError, RepoNotFoundError) as exc:
        print(f"alexandria-web: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
