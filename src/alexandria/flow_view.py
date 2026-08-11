"""Renders the RFC-0007 idea-to-expression flow page.

Server side, this module only serializes a FlowDocument (flow.py) to JSON and
wraps it in a page shell. Every interactive behaviour RFC-0007 specifies --
per-stage expansion levels, sibling compression, keyboard control, URL state,
Markdown export, and the mobile rotation -- lives client-side in the embedded
script, because the flow is inspection over an already-assembled document, not
something that needs a round trip per interaction (RFC-0007 SS05: "Export is
client-side and writes nothing back to the repository").

Deliberately does NOT reuse web.py's `_layout()` -- RFC-0007 SS02 is explicit
that "there is no header chrome beyond the idea line and export control," which
`_layout()`'s app header/nav would violate. This module links the same token
stylesheets `_layout()` does, so it stays visually consistent without
inheriting chrome the spec rules out.
"""

from __future__ import annotations

import html
import json
from typing import Any


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json_for_script(payload: dict[str, Any]) -> str:
    # </script> inside a JSON string would close the tag early; escape it.
    return json.dumps(payload).replace("</", "<\\/")


_STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body.flow-body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif}
.flow-header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;flex-wrap:wrap;
  padding:20px 32px;border-bottom:1px solid var(--border)}
.flow-idea-line{display:flex;flex-direction:column;gap:4px;max-width:70ch}
.flow-idea-line h1{font-size:22px;line-height:1.3;letter-spacing:-.01em;margin:0}
.flow-back{color:var(--accent,#2b50e8);text-decoration:none}
.flow-back:hover{text-decoration:underline}
.flow-meta{font-family:var(--font-mono,monospace);font-size:11.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--text-dim)}
.flow-export{display:flex;flex-direction:column;align-items:flex-end;gap:4px;position:relative}
.ghost-btn{font:inherit;font-family:var(--font-mono,monospace);font-size:11.5px;letter-spacing:.06em;
  text-transform:uppercase;background:transparent;color:var(--text);border:1px solid var(--border);
  border-radius:4px;padding:9px 14px;cursor:pointer}
.ghost-btn:hover{border-color:var(--accent)}
.ghost-btn:focus-visible,a:focus-visible,button:focus-visible,[tabindex]:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px}
.flow-export-scope{font-family:var(--font-mono,monospace);font-size:10.5px;color:var(--text-dim);
  text-transform:uppercase;letter-spacing:.06em}
.export-menu{position:absolute;top:100%;right:0;margin-top:6px;background:var(--bg2);
  border:1px solid var(--border);border-radius:4px;min-width:200px;z-index:20;display:none}
.export-menu.open{display:block}
.export-menu button{display:block;width:100%;text-align:left;background:transparent;border:none;
  color:var(--text);font:inherit;font-size:13px;padding:10px 14px;cursor:pointer}
.export-menu button:hover{background:var(--bg3)}

.flow-rail-wrap{padding:28px 32px 8px;max-width:1440px;margin:0 auto}
.flow-rail{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--border);
  border:1px solid var(--border);border-radius:4px;overflow-x:auto;
  transition:grid-template-columns .15s}
@media(prefers-reduced-motion:reduce){.flow-rail{transition:none}.stage *{transition:none!important}}

.stage{background:var(--bg2);min-width:0;display:flex;flex-direction:column;min-height:148px}
.stage[data-state="not_reached"]{background:var(--bg)}
.stage-head{display:flex;flex-direction:column;gap:8px;padding:16px 18px}
.stage-key{font-family:var(--font-mono,monospace);font-size:11px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--text-dim)}
.stage[data-has-content="true"] .stage-key{color:var(--accent)}
.stage-headline{font-size:16px;font-weight:600;line-height:1.35;margin:0;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.stage.level-1 .stage-headline,.stage.level-2 .stage-headline{font-size:17px}
.stage-not-reached-label{font-family:var(--font-mono,monospace);font-size:11px;color:var(--text-dim);
  text-transform:uppercase;letter-spacing:.06em}
.stage-abandoned-note{border-left:2px solid var(--accent-orange);padding-left:10px;font-size:13px;
  color:var(--text-muted)}
.stage-meta{font-family:var(--font-mono,monospace);font-size:11px;color:var(--text-dim);
  display:flex;flex-wrap:wrap;gap:10px}
.stage-meta .flag{color:var(--accent-orange)}
.level-btn{align-self:flex-start;font-family:var(--font-mono,monospace);font-size:10.5px;
  letter-spacing:.06em;text-transform:uppercase;background:transparent;border:1px solid var(--border);
  border-radius:4px;padding:6px 10px;color:var(--text);cursor:pointer;min-height:28px}
.level-btn:hover{border-color:var(--accent);color:var(--accent)}
.level-btn[data-inert="true"]{cursor:default;border-color:transparent;color:var(--text-dim);padding-left:0}

.stage-key-vertical{writing-mode:vertical-rl;transform:rotate(180deg);font-family:var(--font-mono,monospace);
  font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim);margin:auto;padding:8px 0}
.stage.compressed{align-items:center;justify-content:center;cursor:pointer}
.stage.compressed .stage-head,.stage.compressed .stage-body{display:none}
.stage.compressed .stage-key-vertical{display:block}
.stage:not(.compressed) .stage-key-vertical{display:none}

.stage-body{padding:0 18px 16px;display:flex;flex-direction:column;gap:16px}
.stage.level-0 .stage-body{display:none}
.lanes{display:grid;gap:12px}
.lane{border-top:1px solid var(--border);padding-top:8px}
.lane-label{font-family:var(--font-mono,monospace);font-size:10.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--text-dim)}
.lane-summary{font-size:14px;line-height:1.6;margin-top:3px}
.lane-summary.not-recorded{color:var(--text-dim)}
.lane.accent-orange .lane-label{color:var(--accent-orange)}
.lane.accent-purple .lane-label{color:var(--accent-purple)}
.lane.accent-teal .lane-label{color:var(--accent-teal)}

.excerpt{border-top:1px solid var(--border);padding-top:12px;display:none}
.stage.level-2 .excerpt{display:block}
.excerpt p{font-size:15px;line-height:1.75;max-width:62ch;color:var(--text-muted);margin:0 0 14px}
.excerpt-omitted{font-family:var(--font-mono,monospace);font-size:11px;color:var(--text-dim)}
.excerpt-empty{color:var(--text-dim);font-size:13.5px}

.resolution-pill{display:inline-block;background:var(--bg3);border-radius:4px;padding:4px 9px;
  font-family:var(--font-mono,monospace);font-size:11px;letter-spacing:.06em;text-transform:uppercase}
.resolution-pill.outcome-implemented{color:var(--accent)}
.resolution-pill.outcome-nixed{color:var(--accent-orange)}
.resolution-pill.outcome-morphed{color:var(--accent-purple)}
.forward-pointer{display:inline-flex;align-items:center;gap:4px;color:var(--accent);text-decoration:none}
.forward-pointer:hover{text-decoration:underline}

.stage-footer{display:flex;justify-content:flex-end}
.stage-footer .ghost-btn{padding:6px 10px;font-size:10.5px}

.hairline-wrap{padding:14px 32px 28px;max-width:1440px;margin:0 auto}
.hairline-track{height:1px;background:var(--border);position:relative}
.hairline-fill{height:1px;background:var(--accent);transition:width .15s}
.hairline-count{font-family:var(--font-mono,monospace);font-size:10.5px;color:var(--text-dim);
  margin-top:8px;text-transform:uppercase;letter-spacing:.06em}

@media(max-width:1279px){
  .flow-rail{grid-auto-flow:column;grid-auto-columns:minmax(168px,1fr)}
  .stage{scroll-snap-align:start}
  .flow-rail{scroll-snap-type:x proximity}
}
@media(max-width:767px){
  .flow-header{padding:16px 18px;position:sticky;top:0;background:var(--bg);z-index:10}
  .flow-rail-wrap{padding:16px 18px 4px}
  .flow-rail{display:flex;flex-direction:column;grid-auto-flow:unset}
  .stage.compressed{display:none}
  .level-btn{min-height:44px;width:100%;text-align:center}
  .flow-export{position:fixed;bottom:0;left:0;right:0;background:var(--bg);border-top:1px solid var(--border);
    padding:10px 18px;align-items:stretch;z-index:15}
  .flow-export .ghost-btn{width:100%}
  .hairline-wrap{padding:14px 18px 90px}
}
"""

_SCRIPT = """
(function () {
  var DATA = JSON.parse(document.getElementById('flow-data').textContent);
  var STAGE_ORDER = DATA.stages.map(function (s) { return s.key; });
  var STAGE_NUMBER = {};
  STAGE_ORDER.forEach(function (key, i) { STAGE_NUMBER[key] = String(i + 1).padStart(2, '0'); });
  var levels = {};
  STAGE_ORDER.forEach(function (key) { levels[key] = 0; });

  function isMobile() { return window.matchMedia('(max-width: 767px)').matches; }

  function parseUrlState() {
    var params = new URLSearchParams(window.location.search);
    var open = params.get('open');
    if (!open) return;
    open.split(',').forEach(function (pair) {
      var parts = pair.split(':');
      var num = parts[0], level = parseInt(parts[1], 10);
      var key = STAGE_ORDER[parseInt(num, 10) - 1];
      if (key && (level === 1 || level === 2)) levels[key] = level;
    });
  }

  function writeUrlState() {
    var parts = [];
    STAGE_ORDER.forEach(function (key) {
      if (levels[key] > 0) parts.push(STAGE_NUMBER[key] + ':' + levels[key]);
    });
    var qs = parts.length ? '?open=' + parts.join(',') : window.location.pathname;
    var url = parts.length ? window.location.pathname + '?open=' + parts.join(',') : window.location.pathname;
    history.replaceState(null, '', url);
  }

  function stageByKey(key) { return DATA.stages.filter(function (s) { return s.key === key; })[0]; }

  function laneHtml(lane) {
    var accent = lane.accent ? ' accent-' + lane.accent : '';
    var cls = lane.summary === 'Not recorded' ? ' not-recorded' : '';
    return '<div class="lane' + accent + '"><div class="lane-label">' + esc(lane.label) +
      '</div><div class="lane-summary' + cls + '">' + esc(lane.summary) + '</div></div>';
  }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = String(s == null ? '' : s);
    return d.innerHTML;
  }

  function excerptHtml(stage) {
    if (!stage.excerpt) return '<div class="excerpt"><p class="excerpt-empty">No artifact recorded for this stage.</p></div>';
    var paras = stage.excerpt.paragraphs.map(function (p) { return '<p>' + esc(p) + '</p>'; }).join('');
    var omitted = stage.excerpt.total - stage.excerpt.shown;
    var note = '<div class="excerpt-omitted">' + stage.excerpt.shown + ' of ' + stage.excerpt.total +
      ' paragraphs shown' + (omitted > 0 ? ' &middot; ' + omitted + ' omitted' : '') +
      ' &middot; ' + esc(stage.excerpt.artifact_path) + '</div>';
    return '<div class="excerpt">' + (paras || '<p class="excerpt-empty">Artifact on file has no prose.</p>') + note + '</div>';
  }

  function resolutionLaneHtml(stage) {
    return stage.lanes.map(function (lane) {
      if (lane.label === 'Outcome') {
        var val = lane.summary;
        var tone = val === 'UNRESOLVED' ? '' : (['implemented', 'morphed', 'nixed'].indexOf(val) >= 0 ? ' outcome-' + val : '');
        return '<div class="lane"><div class="lane-label">Outcome</div><div class="lane-summary">' +
          '<span class="resolution-pill' + tone + '">' + esc(val) + '</span></div></div>';
      }
      if (lane.label === 'Expression' && DATA.resolution.outcome === 'morphed' && lane.summary !== 'Not recorded') {
        return '<div class="lane"><div class="lane-label">Expression</div><div class="lane-summary">' +
          '<a class="forward-pointer" href="' + esc(lane.summary) + '">' + esc(lane.summary) + ' &rarr;</a></div></div>';
      }
      return laneHtml(lane);
    }).join('');
  }

  function stageMetaHtml(stage) {
    return stage.meta.map(function (m) {
      var flagged = m.indexOf('failed') >= 0;
      return '<span' + (flagged ? ' class="flag"' : '') + '>' + esc(m) + '</span>';
    }).join('');
  }

  function headHtml(stage, expandedElsewhere) {
    if (stage.state === 'not_reached') {
      return '<div class="stage-head">' +
        '<div class="stage-key">' + STAGE_NUMBER[stage.key] + ' ' + esc(stage.label) + '</div>' +
        '<div class="stage-not-reached-label">Not reached</div>' +
        '<span class="level-btn" data-inert="true">NOT REACHED</span></div>';
    }
    var levelLabel = levels[stage.key] === 0 ? 'EXPAND' : (levels[stage.key] === 1 ? 'MORE' : 'COLLAPSE');
    var abandoned = stage.state === 'abandoned'
      ? '<div class="stage-abandoned-note">Reached, produced nothing.</div>' : '';
    return '<div class="stage-head">' +
      '<div class="stage-key">' + STAGE_NUMBER[stage.key] + ' ' + esc(stage.label) + '</div>' +
      '<h3 class="stage-headline">' + esc(stage.headline || 'Not recorded') + '</h3>' +
      abandoned +
      '<div class="stage-meta">' + stageMetaHtml(stage) + '</div>' +
      '<button type="button" class="level-btn" data-action="level" data-stage="' + stage.key + '">' +
      levelLabel + '</button></div>';
  }

  function bodyHtml(stage) {
    var lanesHtml = stage.key === 'resolution' ? resolutionLaneHtml(stage) : stage.lanes.map(laneHtml).join('');
    var footer = levels[stage.key] > 0
      ? '<div class="stage-footer"><button type="button" class="ghost-btn" data-action="export-stage" data-stage="' +
        stage.key + '">Export stage</button></div>' : '';
    return '<div class="stage-body"><div class="lanes">' + lanesHtml + '</div>' +
      excerptHtml(stage) + footer + '</div>';
  }

  function anyExpanded() { return STAGE_ORDER.some(function (k) { return levels[k] > 0; }); }

  function render() {
    var rail = document.getElementById('flow-rail');
    var hadFocus = rail.contains(document.activeElement);
    var expanded = anyExpanded();
    var cols = STAGE_ORDER.map(function (key) {
      var lvl = levels[key];
      if (lvl === 2) return 'minmax(560px, 3fr)';
      if (lvl === 1) return 'minmax(420px, 2.4fr)';
      return expanded ? '64px' : '1fr';
    });
    if (!isMobile()) rail.style.gridTemplateColumns = cols.join(' ');
    else rail.style.gridTemplateColumns = '';

    rail.innerHTML = DATA.stages.map(function (stage) {
      var lvl = levels[stage.key];
      var compressed = !isMobile() && expanded && lvl === 0;
      var classes = ['stage', 'level-' + lvl];
      if (compressed) classes.push('compressed');
      var reached = stage.state !== 'not_reached';
      return '<div class="' + classes.join(' ') + '" data-state="' + stage.state +
        '" data-has-content="' + reached + '" data-stage="' + stage.key + '" tabindex="0">' +
        '<div class="stage-key-vertical">' + STAGE_NUMBER[stage.key] + ' ' + esc(stage.label) + '</div>' +
        headHtml(stage, expanded) + bodyHtml(stage) + '</div>';
    }).join('');

    Array.prototype.forEach.call(rail.querySelectorAll('.stage.compressed'), function (el) {
      el.addEventListener('click', function () {
        focusedIndex = STAGE_ORDER.indexOf(el.getAttribute('data-stage'));
        setLevel(el.getAttribute('data-stage'), 1);
      });
    });
    Array.prototype.forEach.call(rail.querySelectorAll('[data-action="level"]'), function (btn) {
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var key = btn.getAttribute('data-stage');
        focusedIndex = STAGE_ORDER.indexOf(key);
        var next = levels[key] === 0 ? 1 : (levels[key] === 1 ? 2 : 0);
        setLevel(key, next);
      });
    });
    Array.prototype.forEach.call(rail.querySelectorAll('[data-action="export-stage"]'), function (btn) {
      btn.addEventListener('click', function (ev) { ev.stopPropagation(); exportStage(btn.getAttribute('data-stage')); });
    });

    renderHairline();
    renderExportScope();
    // Rebuilding innerHTML destroys whatever previously held keyboard focus,
    // which silently detaches the keydown listener's effective target (focus
    // falls back to <body>, and subsequent Escape/arrow presses go nowhere).
    // Restore it so keyboard control survives every level change.
    if (hadFocus) focusStage();
  }

  function setLevel(key, level) {
    if (isMobile() && level > 0) {
      STAGE_ORDER.forEach(function (k) { if (k !== key) levels[k] = 0; });
    }
    levels[key] = level;
    writeUrlState();
    render();
  }

  function renderHairline() {
    var reached = DATA.stages.filter(function (s) { return s.state !== 'not_reached'; }).length;
    document.getElementById('hairline-fill').style.width = (reached / 6 * 100) + '%';
    document.getElementById('hairline-count').textContent = reached + ' of 6';
  }

  function renderExportScope() {
    var expandedCount = STAGE_ORDER.filter(function (k) { return levels[k] > 0; }).length;
    var maxLevel = Math.max(0, Object.keys(levels).map(function (k) { return levels[k]; }).reduce(function (a, b) { return Math.max(a, b); }, 0));
    document.getElementById('export-scope').textContent = '6 stages \\u00b7 ' + expandedCount + ' expanded \\u00b7 level ' + maxLevel;
  }

  // ---- Keyboard ----
  var focusedIndex = 0;
  document.getElementById('flow-rail').addEventListener('keydown', function (ev) {
    var stages = STAGE_ORDER;
    if (ev.key === 'ArrowRight') { focusedIndex = Math.min(stages.length - 1, focusedIndex + 1); focusStage(); ev.preventDefault(); }
    else if (ev.key === 'ArrowLeft') { focusedIndex = Math.max(0, focusedIndex - 1); focusStage(); ev.preventDefault(); }
    else if (ev.key === 'ArrowDown') { var k = stages[focusedIndex]; if (stageByKey(k).state !== 'not_reached') setLevel(k, Math.min(2, levels[k] + 1)); ev.preventDefault(); }
    else if (ev.key === 'ArrowUp') { var k2 = stages[focusedIndex]; setLevel(k2, Math.max(0, levels[k2] - 1)); ev.preventDefault(); }
    else if (ev.key === 'Escape') { stages.forEach(function (k3) { levels[k3] = 0; }); writeUrlState(); render(); focusStage(); }
  });
  function focusStage() {
    var els = document.querySelectorAll('#flow-rail .stage');
    if (els[focusedIndex]) els[focusedIndex].focus();
  }

  // ---- Export ----
  function frontMatter() {
    var parts = [];
    STAGE_ORDER.forEach(function (k) { if (levels[k] > 0) parts.push(STAGE_NUMBER[k] + ':' + levels[k]); });
    return '---\\nidea_slug: ' + DATA.idea_slug + '\\nlevels: ' + (parts.join(',') || 'none') + '\\n---\\n\\n';
  }

  function stageMarkdown(stage) {
    var lines = ['## ' + stage.label, ''];
    if (stage.state === 'not_reached') { lines.push('_Not reached._', ''); return lines.join('\\n'); }
    lines.push(stage.headline || '_Not recorded_', '');
    if (levels[stage.key] >= 1) {
      stage.lanes.forEach(function (lane) { lines.push('### ' + lane.label, '', lane.summary, ''); });
    }
    if (levels[stage.key] >= 2 && stage.excerpt) {
      stage.excerpt.paragraphs.forEach(function (p) { lines.push(p, ''); });
      var omitted = stage.excerpt.total - stage.excerpt.shown;
      lines.push('_' + stage.excerpt.shown + ' of ' + stage.excerpt.total + ' paragraphs shown' +
        (omitted > 0 ? ', ' + omitted + ' omitted' : '') + ' \\u00b7 ' + stage.excerpt.artifact_path + '_', '');
    }
    return lines.join('\\n');
  }

  function download(filename, text) {
    var blob = new Blob([text], { type: 'text/markdown' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function maxLevel() {
    return Object.keys(levels).map(function (k) { return levels[k]; }).reduce(function (a, b) { return Math.max(a, b); }, 0);
  }

  function exportFlow() {
    var body = frontMatter() + DATA.stages.map(stageMarkdown).join('\\n');
    download(DATA.idea_slug + '-flow-L' + maxLevel() + '.md', body);
  }

  function exportStage(key) {
    var stage = stageByKey(key);
    var body = frontMatter() + stageMarkdown(stage);
    download(DATA.idea_slug + '-' + key + '-L' + levels[key] + '.md', body);
  }

  document.getElementById('export-flow-btn').addEventListener('click', function (ev) {
    if (DATA.resolution.outcome === 'implemented' && DATA.resolution.expression) {
      ev.stopPropagation();
      var menu = document.getElementById('export-menu');
      menu.classList.toggle('open');
    } else {
      exportFlow();
    }
  });
  var financePiece = document.getElementById('export-finished-piece');
  if (financePiece) financePiece.addEventListener('click', function () {
    window.location.href = DATA.resolution.expression;
  });
  var fullTrace = document.getElementById('export-full-trace');
  if (fullTrace) fullTrace.addEventListener('click', function () { exportFlow(); document.getElementById('export-menu').classList.remove('open'); });
  document.addEventListener('click', function () { document.getElementById('export-menu').classList.remove('open'); });

  parseUrlState();
  render();
  window.addEventListener('resize', render);
})();
"""


def render_flow_page(document_json: dict[str, Any]) -> str:
    title = document_json.get("title") or document_json["idea_slug"]
    opened = document_json.get("opened")
    resolution = document_json.get("resolution") or {}
    has_finished_piece = resolution.get("outcome") == "implemented" and resolution.get("expression")
    export_menu = ""
    if has_finished_piece:
        export_menu = """<div class="export-menu" id="export-menu">
<button type="button" id="export-finished-piece">Finished piece</button>
<button type="button" id="export-full-trace">Full trace</button>
</div>"""
    meta_bits = [f"opened {_e(opened)}"] if opened else []
    meta_bits.append(f"{len(document_json['stages'])} stages")
    body = f"""
<header class="flow-header">
  <div class="flow-idea-line">
    <div class="flow-meta"><a href="/" class="flow-back">&larr; Published work</a></div>
    <div class="flow-meta">{_e(document_json["idea_slug"])}</div>
    <h1>{_e(title)}</h1>
    <div class="flow-meta">{" &middot; ".join(_e(b) for b in meta_bits)}</div>
  </div>
  <div class="flow-export">
    <button type="button" class="ghost-btn" id="export-flow-btn">Export Markdown</button>
    <div class="flow-export-scope" id="export-scope"></div>
    {export_menu}
  </div>
</header>
<div class="flow-rail-wrap">
  <div class="flow-rail" id="flow-rail"></div>
</div>
<div class="hairline-wrap">
  <div class="hairline-track"><div class="hairline-fill" id="hairline-fill"></div></div>
  <div class="hairline-count" id="hairline-count"></div>
</div>
<script id="flow-data" type="application/json">{_json_for_script(document_json)}</script>
<script>{_SCRIPT}</script>
"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} &middot; Alexandria flow</title>
<link rel="stylesheet" href="/assets/tokens/fonts.css"><link rel="stylesheet" href="/assets/tokens/colors.css">
<link rel="stylesheet" href="/assets/tokens/typography.css"><link rel="stylesheet" href="/assets/tokens/spacing.css">
<link rel="stylesheet" href="/assets/tokens/layout.css"><link rel="stylesheet" href="/assets/tokens/base.css">
<link rel="stylesheet" href="/assets/styles.css">
<style>{_STYLE}</style>
</head><body class="flow-body">{body}</body></html>"""
