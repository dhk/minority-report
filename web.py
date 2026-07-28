"""Local single-operator web surface for commissioning Alexandria research."""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections.abc import Sequence
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
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
from alexandria.infrastructure.config import Config, load_config
from alexandria.infrastructure.secrets import SecretNotFoundError, openrouter_api_key
from alexandria.input_resolution import (
    GitHubResolver,
    InputResolutionError,
    extract_input,
    pasted_input,
    validate_input_set,
)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _layout(body: str, *, title: str = "Alexandria") -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{_e(title)}</title>
<link rel="stylesheet" href="/assets/tokens/fonts.css"><link rel="stylesheet" href="/assets/tokens/colors.css">
<link rel="stylesheet" href="/assets/tokens/typography.css"><link rel="stylesheet" href="/assets/tokens/spacing.css">
<link rel="stylesheet" href="/assets/tokens/layout.css"><link rel="stylesheet" href="/assets/tokens/base.css">
<link rel="stylesheet" href="/assets/styles.css">
<style>
body{{background:var(--bg);color:var(--text);font-family:system-ui;margin:0}} header{{display:flex;justify-content:space-between;padding:18px 32px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:2}} main{{max-width:1180px;margin:auto;padding:48px 32px}} .mono{{font-family:var(--font-mono);text-transform:uppercase;letter-spacing:.08em;font-size:11px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:56px}} .card{{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:22px}} label{{display:block;font-weight:600;margin-top:18px}} textarea,input{{box-sizing:border-box;width:100%;margin-top:7px;padding:11px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text)}} textarea{{min-height:92px}} button,.button{{display:inline-block;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:white;padding:12px 18px;text-decoration:none;font:inherit;margin-top:18px}} button[disabled]{{opacity:.45}} .ghost{{background:transparent;color:var(--text)}} .warning{{border-left:3px solid var(--accent-orange);padding-left:12px}} table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;border-bottom:1px solid var(--border);padding:12px 8px;vertical-align:top}} .score{{font-family:var(--font-mono);text-align:center}} .failed{{color:var(--accent-orange)}} .silent{{color:var(--text-dim)}} .pill{{display:inline-block;background:var(--bg3);border-radius:4px;padding:4px 7px}} details{{margin:12px 0}} pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg2);padding:16px;border:1px solid var(--border)}} @media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><header><div><strong>Alexandria</strong> <span class="mono">Research commissions</span></div><nav><a href="/">History / New commission</a></nav></header><main>{body}</main></body></html>"""


def _input_rows(inputs: Sequence[InputArtifact]) -> str:
    rows = []
    for item in inputs:
        warning = f'<div class="warning">{_e(item.warning)}</div>' if item.warning else ""
        rows.append(
            f"<tr><td>{_e(item.name)}{warning}</td><td>{_e(item.state)}</td>"
            f'<td class="mono">{item.bytes} bytes · {item.extracted_chars} chars · {_e(item.sha256[:12])}</td></tr>'
        )
    return "".join(rows)


def _history(store: RunStore) -> str:
    runs = store.list_runs()
    if not runs:
        return "<p>No commissioned runs yet.</p>"
    rows = "".join(
        f'<tr><td><a href="/runs/{_e(run.run_id)}">{_e(run.run_id)}</a></td>'
        f"<td>{_e(run.status)}</td><td>{len(run.inputs)}</td><td>{len(run.dispatched_models)}</td>"
        f"<td>{'$' + format(run.cost_actual, '.4f') if run.cost_actual is not None else '—'}</td></tr>"
        for run in runs
    )
    return f"<table><thead><tr><th>Run</th><th>Status</th><th>Inputs</th><th>Models</th><th>Cost</th></tr></thead><tbody>{rows}</tbody></table>"


async def homepage(request: Request) -> HTMLResponse:
    config: Config = request.app.state.config
    models = "\n".join(DEFAULT_MODELS)
    body = f"""
<h1>Commission the same question to several models.</h1>
<p>Keep what each one said. Agreement is model agreement. It is not verification.</p>
<section class="card"><form action="/review" method="post" enctype="multipart/form-data">
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
<button type="submit">Review commission →</button></div></div></form></section>
<h2>Run history</h2>{_history(RunStore(config.data_dir))}
"""
    return HTMLResponse(_layout(body))


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
        estimate = f"${draft.estimate_usd:.4f} maximum"
        blocked = draft.estimate_usd > draft.ceiling_usd
        price_note = (
            f"Hard ceiling: ${draft.ceiling_usd:.2f}."
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
<aside class="card"><div class="mono">Estimated maximum</div><h2>{_e(estimate)}</h2><p>{_e(price_note)}</p>
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


async def result(request: Request) -> HTMLResponse:
    config: Config = request.app.state.config
    try:
        store = RunStore(config.data_dir)
        run = store.load_run(request.path_params["run_id"])
        claims, scores = _load_result(config, run)
    except (CommissionError, OSError, ValueError) as exc:
        return HTMLResponse(_layout(f"<h1>Run unavailable</h1><p>{_e(exc)}</p>"), status_code=404)
    score_map = {(row["claim_id"], row["model_id"]): row for row in scores}
    headers = "".join(f"<th>{_e(model)}</th>" for model in run.dispatched_models)
    rows = []
    for claim in claims:
        cells = []
        for model in run.dispatched_models:
            row = score_map.get((claim["claim_id"], model))
            raw_score = row.get("score") if row else None
            if raw_score in {None, ""}:
                value, css, title = "✕", "failed", "Call failed; no output exists to grade."
            elif int(raw_score) == 0:
                value, css, title = "—", "silent", "Model responded; no bearing statement found."
            else:
                assert row is not None
                value, css, title = f"{int(raw_score):+d}", "", row.get("quote") or ""
            cells.append(f'<td class="score {css}" title="{_e(title)}">{_e(value)}</td>')
        rows.append(
            f'<tr><td>{_e(claim["text"])}<br><span class="pill mono">{_e(claim["group"])} · {claim["responding_model_count"]}/{len(run.dispatched_models)}</span></td>{"".join(cells)}</tr>'
        )
    limitations = (
        "".join(f"<li>{_e(item)}</li>" for item in run.limitations)
        or "<li>Agreement is not verification.</li>"
    )
    raw_sections = []
    for model in run.dispatched_models:
        path = store.run_dir(run.run_id) / "raw" / f"{model.replace('/', '-')}.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw_sections.append(
                f"<details><summary>{_e(model)}</summary><pre>{_e(payload.get('raw_response') or payload.get('error') or '')}</pre></details>"
            )
    body = f"""<div class="mono">04 — Result</div><h1>{_e(run.run_id)} · brief {run.brief_revision} · {_e(run.status)}</h1>
<p>{len(run.inputs)} inputs · {len(run.dispatched_models)} models · cost {_e("$" + format(run.cost_actual, ".4f") if run.cost_actual is not None else "unavailable")} · elapsed {_e(run.elapsed_seconds or "—")}s</p>
<section class="warning"><strong>Limitations</strong><ul>{limitations}</ul></section>
<h2>Claim landscape</h2><table><thead><tr><th>Claim</th>{headers}</tr></thead><tbody>{"".join(rows) or "<tr><td>No validated claims are available.</td></tr>"}</tbody></table>
<h2>Raw outputs</h2>{"".join(raw_sections)}<p>Agreement is model agreement. It is not verification.</p>"""
    return HTMLResponse(_layout(body, title=f"{run.run_id} · Alexandria"))


def create_app(config: Config | None = None) -> Starlette:
    config = config or load_config()
    routes: list[Any] = [
        Route("/", homepage),
        Route("/review", review, methods=["POST"]),
        Route("/dispatch/{draft_id}", dispatch, methods=["POST"]),
        Route("/runs/{run_id}", result),
    ]
    matches = list((config.repo_root / "docs/ux/prototype/_ds").glob("dhk-design-system-*"))
    if matches:
        routes.append(Mount("/assets", app=StaticFiles(directory=matches[0]), name="assets"))
    app = Starlette(routes=routes)
    app.state.config = config
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="alexandria-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8042)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port)
