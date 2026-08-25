"""Dispatch that returns before the commission finishes (#33)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from alexandria import background
from alexandria.commission import (
    ASSUMED_COMPLETION_TOKENS,
    CommissionError,
    CommissionService,
    RunStore,
)
from alexandria.commission_models import Brief, CallRecord
from alexandria.infrastructure.config import Config
from alexandria.input_resolution import extract_input

pytestmark = pytest.mark.anyio


class FakeGateway:
    """Enough of a gateway to finish a commission. Local by design: importing
    it from another test module gives mypy two names for one file."""

    async def estimate(
        self,
        models: list[str],
        input_tokens: int,
        *,
        web_search: bool = False,
        completion_tokens: int = ASSUMED_COMPLETION_TOKENS,
    ) -> float:
        return 0.25

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        if model == "grader/model":
            body = json.dumps(
                {
                    "claims": [
                        {
                            "text": "The provider port should remain narrow.",
                            "scores": [{"model_index": 1, "score": 3, "quote": "narrow"}],
                        }
                    ],
                    "report_markdown": "# Report\n\n## What this run does not establish\n\nTruth.",
                }
            )
        else:
            body = "Evidence says the provider port should remain narrow."
        return CallRecord(
            model_id=model,
            resolved_model_id=model + ":resolved",
            status="success",
            body=body,
            raw_response=json.dumps({"model": model, "body": body}),
            generation_id="gen-" + model.replace("/", "-"),
            cost=0.01,
            latency_ms=10,
        )


class SlowGateway(FakeGateway):
    """A commission that takes long enough to prove the caller was not waiting."""

    def __init__(self, release: asyncio.Event) -> None:
        self.release = release

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        await self.release.wait()
        return await super().complete(model, prompt, web_search=web_search)


def _config(tmp_path: Path) -> Config:
    (tmp_path / "repo" / "research").mkdir(parents=True)
    return Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=tmp_path / "repo",
        repo_root_source="test",
    )


async def _draft(config: Config, gateway: FakeGateway, ceiling: float = 1.0) -> str:
    draft = await CommissionService(config, gateway).create_draft(
        Brief(task="Assess the provider port."),
        [extract_input("brief.md", b"Keep the port narrow.")],
        ["alpha/model", "beta/model"],
        "grader/model",
        ceiling,
    )
    return draft.draft_id


async def _start(config: Config, gateway: FakeGateway, draft_id: str) -> object:
    """Exercise the real launcher, with the gateway injected rather than live."""

    @asynccontextmanager
    async def _cm() -> AsyncIterator[FakeGateway]:
        yield gateway

    return await background.start(config, draft_id, gateway_cm=_cm)


async def test_the_caller_gets_a_run_id_before_the_models_are_called(tmp_path: Path) -> None:
    """The whole point: a reply that does not wait cannot be lost to a timeout."""
    config = _config(tmp_path)
    release = asyncio.Event()
    gateway = SlowGateway(release)
    draft_id = await _draft(config, gateway)

    run = await asyncio.wait_for(_start(config, gateway, draft_id), timeout=5)

    assert run.run_id  # type: ignore[attr-defined]
    assert not release.is_set(), "no model was called before the caller was answered"
    release.set()
    await asyncio.sleep(0)


async def test_the_run_is_findable_the_moment_dispatch_returns(tmp_path: Path) -> None:
    """A record that appears only at the end is a record a timeout can lose."""
    config = _config(tmp_path)
    release = asyncio.Event()
    gateway = SlowGateway(release)
    draft_id = await _draft(config, gateway)

    run = await _start(config, gateway, draft_id)
    stored = RunStore(config.data_dir).load_run(run.run_id)  # type: ignore[attr-defined]

    assert stored.status == "running"
    assert stored.dispatched_models == ["alpha/model", "beta/model"]
    release.set()


async def test_the_commission_finishes_after_the_caller_has_gone(tmp_path: Path) -> None:
    config = _config(tmp_path)
    release = asyncio.Event()
    gateway = SlowGateway(release)
    draft_id = await _draft(config, gateway)

    run = await _start(config, gateway, draft_id)
    release.set()
    for _ in range(200):  # let the detached task run to completion
        await asyncio.sleep(0.01)
        if RunStore(config.data_dir).load_run(run.run_id).status != "running":  # type: ignore[attr-defined]
            break

    assert RunStore(config.data_dir).load_run(run.run_id).status == "completed"  # type: ignore[attr-defined]


async def test_a_failure_before_any_spend_still_reaches_the_caller(tmp_path: Path) -> None:
    """Nothing was spent, so this must fail loudly rather than in the background."""
    config = _config(tmp_path)
    gateway = FakeGateway()
    draft_id = await _draft(config, gateway, ceiling=0.0001)

    # Matches the stable half of the message. The wording around it changed in
    # #56 ("make the ceiling a bound"), which is what broke the old
    # "exceeds ceiling" regex — the amounts are interpolated, so anchoring on
    # them would break again the moment a default moves.
    with pytest.raises(CommissionError, match="exceeds the .* ceiling"):
        await _start(config, gateway, draft_id)


async def test_in_flight_reports_only_this_process(tmp_path: Path) -> None:
    """A run missing from this set may still be alive in another server."""
    config = _config(tmp_path)
    release = asyncio.Event()
    gateway = SlowGateway(release)
    draft_id = await _draft(config, gateway)

    run = await _start(config, gateway, draft_id)

    assert run.run_id in background.in_flight_run_ids()  # type: ignore[attr-defined]
    assert background.describe(config, run.run_id) == "running here now"  # type: ignore[attr-defined]
    release.set()


async def test_a_run_this_process_does_not_own_is_not_claimed_as_finished(
    tmp_path: Path,
) -> None:
    """Absence from in-flight is not evidence a run died — another server may have it."""
    config = _config(tmp_path)
    gateway = FakeGateway()
    draft_id = await _draft(config, gateway)
    prepared = CommissionService(config, gateway).prepare(draft_id)

    described = background.describe(config, prepared.run.run_id)

    assert "not by this process" in described
    assert "another server, or interrupted" in described
