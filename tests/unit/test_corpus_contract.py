"""Conformance checks against contracts the research corpus owns.

The corpus (dhk/alexandria) defines what a valid artifact looks like; this
repository writes those artifacts. Before the split those lived in one tree and
a contract change was hard to miss. Now nothing connects them, so a change on
either side can silently produce artifacts the other rejects -- dhk/alexandria#33's
fourth open question.

Two tiers, deliberately:

* Checks that need no corpus run always, in CI included. They enforce that this
  repository is internally consistent with the contract as mirrored here.
* Checks that compare against the corpus's own schema files run only when a
  corpus checkout is reachable, and SKIP loudly otherwise. A skip is not a pass;
  CI has no corpus, so cross-repo drift is caught locally and on the host, not
  on every push. Closing that gap needs the corpus available to CI, which is a
  separate decision.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from alexandria.commission import SCORE_MAX, SCORE_MIN
from alexandria.commission_models import ScoreRecord

_CLAIM_SCORE_SCHEMA = Path("schemas/claim-score.schema.json")


def _corpus_root() -> Path | None:
    """Find a corpus checkout, or None. Never guesses a path that might be wrong."""
    configured = os.environ.get("ALEXANDRIA_REPO", "").strip()
    candidates = [Path(configured)] if configured else []
    # A sibling checkout is the layout the owner's machines actually use.
    candidates.append(Path(__file__).resolve().parents[3] / "alexandria")
    for candidate in candidates:
        if (candidate / _CLAIM_SCORE_SCHEMA).is_file():
            return candidate
    return None


def _claim_score_schema() -> dict[str, Any]:
    corpus = _corpus_root()
    if corpus is None:
        pytest.skip(
            "No corpus checkout found: set ALEXANDRIA_REPO or place a dhk/alexandria "
            "checkout beside this repo. This is a SKIP, not a pass -- the cross-repo "
            "contract was not checked."
        )
    loaded = json.loads((corpus / _CLAIM_SCORE_SCHEMA).read_text(encoding="utf-8"))
    return cast(dict[str, Any], loaded)


def test_mirrored_score_bounds_match_the_corpus_schema() -> None:
    schema = _claim_score_schema()

    assert schema["minimum"] == SCORE_MIN
    assert schema["maximum"] == SCORE_MAX


def test_the_corpus_still_defines_every_score_this_repo_can_emit() -> None:
    schema = _claim_score_schema()
    allowed = {entry["const"] for entry in schema["oneOf"]}

    assert allowed == set(range(SCORE_MIN, SCORE_MAX + 1))


def test_the_corpus_still_reserves_zero_for_graded_silence() -> None:
    # The load-bearing distinction in the whole scoring model: 0 means the model
    # answered and said nothing bearing on the claim. A failed call has no score
    # at all. If the corpus ever redefines 0, this repository's failed-call
    # handling becomes wrong and must change with it.
    schema = _claim_score_schema()
    zero = next(entry for entry in schema["oneOf"] if entry["const"] == 0)

    assert "silent" in str(zero["description"]).lower()
    assert "never be coerced to 0" in str(schema["description"])


def test_a_failed_call_has_no_score_rather_than_a_zero_one() -> None:
    # Runs without a corpus: this is the invariant the schema exists to protect,
    # asserted against this repository's own model.
    failed = ScoreRecord(
        claim_id="c-001", model_id="alpha/model", score=None, quote=None, grading_call_id=None
    )
    silent = ScoreRecord(
        claim_id="c-001", model_id="beta/model", score=0, quote=None, grading_call_id="g-1"
    )

    assert failed.score is None
    assert silent.score == 0
    assert failed.score != silent.score


@pytest.mark.parametrize("score", list(range(SCORE_MIN, SCORE_MAX + 1)))
def test_every_in_range_score_is_representable(score: int) -> None:
    record = ScoreRecord(
        claim_id="c-001",
        model_id="alpha/model",
        score=score,
        quote="quote" if score else None,
        grading_call_id="g-1",
    )

    assert SCORE_MIN <= (record.score or 0) <= SCORE_MAX
