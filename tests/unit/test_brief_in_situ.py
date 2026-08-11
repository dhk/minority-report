"""The brief coloured by what it provoked (#37)."""

from __future__ import annotations

from typing import Any

from alexandria.brief_in_situ import attribute, render

BRIEF = """Assess whether the provider port should remain narrow.

Consider the cost of onboarding a second payment vendor next year.

Out of scope: pricing, and anything about the mobile client.
"""


def _claim(text: str, group: str = "consensus", models: int = 3) -> dict[str, Any]:
    return {"claim_id": text[:8], "text": text, "group": group, "responding_model_count": models}


def test_a_claim_lands_on_the_paragraph_it_shares_language_with() -> None:
    result = attribute(BRIEF, [_claim("The provider port should remain narrow.")])

    assert len(result.paragraphs) == 3
    assert len(result.paragraphs[0].claims) == 1
    assert result.unanchored == []


def test_a_claim_that_matches_nothing_is_shown_not_dropped() -> None:
    """An invisible omission reads as a claim never made."""
    result = attribute(BRIEF, [_claim("Kubernetes autoscaling behaves unpredictably.")])

    assert result.anchored_count == 0
    assert len(result.unanchored) == 1


def test_a_coincidental_single_word_is_not_treated_as_attribution() -> None:
    """One shared word is coincidence; calling it provenance would be a lie."""
    result = attribute(
        "Assess the provider port.\n\nUnrelated second paragraph.",
        [_claim("Vendor pricing varies by provider.")],
    )

    assert result.anchored_count == 0


def test_disagreement_wins_a_tie_on_a_paragraph() -> None:
    """A passage that produced any contested claim is more useful flagged contested."""
    result = attribute(
        BRIEF,
        [
            _claim("The provider port should remain narrow.", group="consensus"),
            _claim("The provider port cannot remain narrow.", group="disagreement"),
        ],
    )

    assert result.paragraphs[0].dominant_group == "disagreement"


def test_the_document_says_attribution_is_inferred() -> None:
    """The honesty line is the point: this is a guess about provenance."""

    class _Run:
        run_id = "r-2026-0810-01"

    html = render(_Run(), BRIEF, [_claim("The provider port should remain narrow.")])

    assert "inferred, not recorded" in html
    assert "paragraph resolution" in html
    assert "not verification" in html


def test_the_document_is_self_contained() -> None:
    """Same rule as the bundled heatmap: it must work as a file with no server."""

    class _Run:
        run_id = "r-2026-0810-01"

    html = render(_Run(), BRIEF, [_claim("The provider port should remain narrow.")])

    assert "<style>" in html
    for external in ("<link", "<script", "src=", "https://", "http://"):
        assert external not in html, external


def test_brief_text_is_escaped_not_injected() -> None:
    class _Run:
        run_id = "r"

    html = render(_Run(), "A brief with <script>alert(1)</script> in it.", [])

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_every_claim_appears_somewhere(  # rule 5, applied to this surface
) -> None:
    class _Run:
        run_id = "r"

    claims = [
        _claim("The provider port should remain narrow."),
        _claim("Onboarding a second payment vendor costs more than expected."),
        _claim("Entirely unrelated to anything asked."),
    ]
    result = attribute(BRIEF, claims)
    html = render(_Run(), BRIEF, claims)

    assert result.anchored_count + len(result.unanchored) == 3
    for claim in claims:
        assert claim["text"] in html


def test_a_long_paragraph_does_not_become_a_magnet() -> None:
    """Raw word counts made the longest paragraph absorb almost every claim.

    Measured on a real commission: 36 of 40 claims landed on one 125-word
    paragraph while 26-to-55-word paragraphs took nearly none.
    """
    long_para = " ".join(["provider port narrow vendor payment onboarding cost"] * 12)
    brief = f"{long_para}\n\nA short paragraph about mobile client latency."
    claims = [_claim("Mobile client latency is worse than expected.")]

    result = attribute(brief, claims)

    assert len(result.paragraphs[1].claims) == 1, "the short, on-topic paragraph should win"
    assert result.paragraphs[0].claims == []


def test_a_verified_span_places_a_claim_rather_than_word_overlap() -> None:
    """Provenance beats resemblance: a recorded anchor is not a guess."""
    claim = _claim("Something with no words in common with its own paragraph.")
    claim["brief_quote"] = "anything about the mobile client"

    result = attribute(BRIEF, [claim])

    assert result.recorded == 1
    assert len(result.paragraphs[2].claims) == 1, "placed by the span, not by overlap"


def test_an_anchor_that_is_not_in_the_brief_falls_back_to_inference() -> None:
    """A discarded anchor must not silently become a confident placement."""
    claim = _claim("The provider port should remain narrow.")
    claim["brief_quote"] = "a sentence the brief never contained"

    result = attribute(BRIEF, [claim])

    assert result.recorded == 0
    assert result.anchored_count == 1, "still placed, but by inference"


def test_the_document_distinguishes_recorded_from_inferred() -> None:
    class _Run:
        run_id = "r"

    recorded = _claim("Anything at all.")
    recorded["brief_quote"] = "anything about the mobile client"
    mixed = render(_Run(), BRIEF, [recorded, _claim("The provider port should remain narrow.")])
    inferred_only = render(_Run(), BRIEF, [_claim("The provider port should remain narrow.")])
    recorded_only = render(_Run(), BRIEF, [recorded])

    assert "Mixed provenance" in mixed
    assert "inferred, not recorded" in inferred_only
    assert "recorded, not inferred" in recorded_only
