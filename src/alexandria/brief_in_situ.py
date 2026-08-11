"""The brief you wrote, coloured by what turned out to be contested (#37).

The heatmap answers "what did the models say about each claim". This answers
"which parts of what I asked turned out to be contested" — the same evidence,
read against the question instead of against a list.

**Attribution here is inferred, not recorded.** ``ClaimRecord`` carries no span,
offset, or source reference tying a claim back to the brief text it came from,
so nothing in the run says which paragraph produced which claim. This matches
claims to paragraphs by content-word overlap and says so on the face of the
document.

Two consequences, both deliberate:

* **Paragraph granularity, not sentence.** Fuzzy-matching a claim to a
  *sentence* would be precise-looking and sometimes wrong, and a confident wrong
  highlight on a provenance surface is worse than a coarse right one.
* **Nothing is dropped.** A claim that matches no paragraph is shown in its own
  section rather than silently omitted — an invisible omission would read as
  "the brief produced no such claim".

The honest fix is anchoring at extraction time, which would make this exact and
retire the inference. Until then this says what it is.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from html import escape
from typing import Any

# Words too common to carry attribution signal. Deliberately short: a longer
# list starts encoding opinions about the subject matter.
_NOISE = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "must",
        "no",
        "not",
        "of",
        "on",
        "or",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)

# Below this, a match is coincidence rather than attribution. Two shared content
# words is the floor at which a claim is plausibly about a paragraph.
_MIN_SHARED_WORDS = 2


def _content_words(text: str) -> Counter[str]:
    words = re.findall(r"[a-z][a-z'-]{2,}", text.lower())
    return Counter(word for word in words if word not in _NOISE)


@dataclass
class AnchoredParagraph:
    text: str
    claims: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dominant_group(self) -> str:
        """The group that most defines this passage.

        Disagreement wins ties: a paragraph that produced any contested claim is
        more usefully flagged as contested than as agreed.
        """
        if not self.claims:
            return "none"
        groups = [str(claim.get("group", "silent")) for claim in self.claims]
        if "disagreement" in groups:
            return "disagreement"
        return Counter(groups).most_common(1)[0][0]


@dataclass
class Attribution:
    paragraphs: list[AnchoredParagraph]
    unanchored: list[dict[str, Any]]

    @property
    def anchored_count(self) -> int:
        return sum(len(paragraph.claims) for paragraph in self.paragraphs)


def attribute(brief_text: str, claims: list[dict[str, Any]]) -> Attribution:
    """Match each claim to the brief paragraph it most plausibly came from."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", brief_text) if block.strip()]
    paragraphs = [AnchoredParagraph(text=block) for block in blocks]
    profiles = [_content_words(block) for block in blocks]

    # Normalised by paragraph length. On a real brief, raw shared-word counts
    # made the longest paragraph a magnet: 36 of 40 claims landed on a
    # 125-word paragraph while 26-to-55-word paragraphs took almost none.
    # Dividing by sqrt(size) keeps the substantive paragraph dominant without
    # letting length alone win.
    sizes = [max(sum(profile.values()), 1) for profile in profiles]

    unanchored: list[dict[str, Any]] = []
    for claim in claims:
        claim_words = _content_words(str(claim.get("text", "")))
        best_index, best_score = -1, 0.0
        for index, profile in enumerate(profiles):
            shared = sum((claim_words & profile).values())
            if shared < _MIN_SHARED_WORDS:
                continue
            score = shared / math.sqrt(sizes[index])
            if score > best_score:
                best_index, best_score = index, score
        if best_index >= 0:
            paragraphs[best_index].claims.append(claim)
        else:
            unanchored.append(claim)
    return Attribution(paragraphs=paragraphs, unanchored=unanchored)


def _claim_chip(claim: dict[str, Any]) -> str:
    group = escape(str(claim.get("group", "silent")))
    text = escape(str(claim.get("text", "")))
    count = claim.get("responding_model_count")
    models = f"{count} model(s) answered" if count is not None else "no model count recorded"
    return (
        f'<li class="chip group-{group}"><span class="chip-group">{group}</span>'
        f"<span class='chip-text'>{text}</span>"
        f'<span class="chip-models">{escape(models)}</span></li>'
    )


def render(run: Any, brief_text: str, claims: list[dict[str, Any]]) -> str:
    """A self-contained document: the brief, coloured by what it provoked.

    Standalone by construction — no external CSS, fonts, or scripts — so it
    works as a file with no server behind it, the same rule the bundled heatmap
    follows.
    """
    attribution = attribute(brief_text, claims)

    body = []
    for paragraph in attribution.paragraphs:
        group = paragraph.dominant_group
        chips = "".join(_claim_chip(claim) for claim in paragraph.claims)
        claim_list = f'<ul class="chips">{chips}</ul>' if chips else ""
        label = (
            f'<span class="para-label group-{escape(group)}">{escape(group)}</span>'
            if group != "none"
            else '<span class="para-label quiet">no claims traced here</span>'
        )
        body.append(
            f'<section class="para group-{escape(group)}">'
            f"{label}<p>{escape(paragraph.text)}</p>{claim_list}</section>"
        )

    if attribution.unanchored:
        chips = "".join(_claim_chip(claim) for claim in attribution.unanchored)
        body.append(
            '<section class="para unanchored">'
            '<span class="para-label quiet">not traced to any paragraph</span>'
            "<p>These claims could not be matched to a passage of the brief. They are "
            "shown because a claim that vanished would read as a claim never made — "
            "not because the brief is silent on them.</p>"
            f'<ul class="chips">{chips}</ul></section>'
        )

    traced = attribution.anchored_count
    total = traced + len(attribution.unanchored)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Brief in situ — {escape(str(getattr(run, "run_id", "")))}</title>
<style>
:root{{color-scheme:light dark}}
body{{margin:0;font:16px/1.7 ui-serif,Georgia,serif;background:#fbfaf8;color:#1a1a1a}}
@media(prefers-color-scheme:dark){{body{{background:#14161a;color:#e8e8e8}}}}
main{{max-width:64rem;margin:auto;padding:40px 24px 80px}}
h1{{font:600 26px/1.3 ui-sans-serif,system-ui;margin:0 0 6px}}
.meta{{font:12px ui-monospace,monospace;color:#6b7280;margin-bottom:26px}}
.caveat{{border-left:3px solid #e05c2a;background:rgba(224,92,42,.07);padding:14px 18px;
  margin:0 0 34px;font:14px/1.6 ui-sans-serif,system-ui}}
.para{{border-left:5px solid #d8d5cf;padding:16px 20px;margin:0 0 20px;border-radius:4px;
  background:rgba(0,0,0,.02)}}
@media(prefers-color-scheme:dark){{.para{{background:rgba(255,255,255,.03);border-left-color:#3a3f47}}}}
.para p{{margin:8px 0 0;white-space:pre-wrap}}
.para.group-consensus{{border-left-color:#2b50e8;background:rgba(43,80,232,.07)}}
.para.group-disagreement{{border-left-color:#e05c2a;background:rgba(224,92,42,.08)}}
.para.group-novel{{border-left-color:#7a4bbe;background:rgba(122,75,190,.08)}}
.para.group-thin{{border-left-color:#1f8b86;background:rgba(31,139,134,.08)}}
.para.group-silent{{border-left-color:#8b8b8b}}
.para-label{{font:11px/1 ui-monospace,monospace;letter-spacing:.09em;text-transform:uppercase}}
.para-label.quiet{{color:#8b8b8b}}
.group-consensus{{color:#2b50e8}} .group-disagreement{{color:#e05c2a}}
.group-novel{{color:#7a4bbe}} .group-thin{{color:#1f8b86}} .group-silent{{color:#6b7280}}
.chips{{list-style:none;margin:14px 0 0;padding:0;display:grid;gap:8px}}
.chip{{border:1px solid rgba(128,128,128,.35);border-radius:4px;padding:9px 12px;
  font:14px/1.5 ui-sans-serif,system-ui;background:rgba(255,255,255,.5)}}
@media(prefers-color-scheme:dark){{.chip{{background:rgba(0,0,0,.25)}}}}
.chip-group{{font:10px ui-monospace,monospace;letter-spacing:.09em;text-transform:uppercase;
  display:block;margin-bottom:4px}}
.chip-models{{display:block;font:11px ui-monospace,monospace;color:#6b7280;margin-top:5px}}
.unanchored{{border-left-style:dashed}}
</style></head><body><main>
<h1>The brief, in situ</h1>
<p class="meta">Run {escape(str(getattr(run, "run_id", "")))} &middot;
{traced} of {total} claims traced to a passage</p>
<p class="caveat"><strong>Attribution here is inferred, not recorded.</strong>
Claims carry no reference back to the text that produced them, so each one is
matched to the paragraph it shares the most content words with. That is a guess
about provenance, at paragraph resolution. Colour shows which passages provoked
agreement or disagreement — it does not show that a model was reading that
passage. Agreement is model agreement, not verification.</p>
{"".join(body)}
</main></body></html>
"""
