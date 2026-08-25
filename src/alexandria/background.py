"""Dispatching a commission without making the caller wait for it (#33).

``run_research`` used to block for the whole commission — every model, web
search, grading — which routinely outran a 60s MCP client timeout. The reply
was the only signal a caller got, so losing it lost the run.

The work is now detached from whoever asked for it. ``start`` prepares the run
(which writes the record and costs nothing), hands back the id, and lets the
model calls finish in the background. Every result is written to the run store
rather than returned, so a caller who has gone away costs nothing.

**What this does not do:** reconcile a run orphaned by a server restart. Both
the MCP server and the web surface can dispatch, so a sweep that marked every
``running`` record failed on startup would mislabel the *other* process's live
run. ``run_status`` flags a suspiciously old run instead, and marking one failed
stays a deliberate act.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from alexandria.commission import CommissionService, OpenRouterGateway, RunStore
from alexandria.infrastructure.config import Config
from alexandria.infrastructure.secrets import openrouter_api_key

if TYPE_CHECKING:
    from alexandria.commission_models import RunRecord

_log = logging.getLogger(__name__)

# asyncio keeps only a weak reference to a running task, so a task nobody holds
# can be garbage-collected mid-flight — a commission that silently stops paying
# attention to itself. Holding them here is what keeps them alive.
_in_flight: set[asyncio.Task[None]] = set()


def in_flight_run_ids() -> set[str]:
    """Runs this process is currently executing.

    Only this process: another server's runs are live but invisible here, which
    is why absence from this set means nothing on its own.
    """
    return {
        task.get_name().removeprefix("dispatch:")
        for task in _in_flight
        if not task.done() and task.get_name().startswith("dispatch:")
    }


@asynccontextmanager
async def _live_gateway() -> AsyncIterator[Any]:
    async with OpenRouterGateway(openrouter_api_key()) as gateway:
        yield gateway


async def _execute(
    config: Config,
    prepared_ready: asyncio.Future[RunRecord],
    draft_id: str,
    gateway_cm: Callable[[], AbstractAsyncContextManager[Any]],
) -> None:
    """Own the gateway for the whole commission, not just the reply."""
    try:
        async with gateway_cm() as gateway:
            service = CommissionService(config, gateway)
            prepared = service.prepare(draft_id)
            if not prepared_ready.done():
                prepared_ready.set_result(prepared.run)
            await service.execute(prepared)
    except Exception as exc:  # noqa: BLE001 — a detached task must not die silently
        if not prepared_ready.done():
            # Failed before the caller got an id: they are still waiting, so
            # give them the reason rather than a timeout.
            prepared_ready.set_exception(exc)
        else:
            # The caller already left with an id. The run record is the only
            # place this can be reported, and it stays as written — see the
            # module docstring on why nothing sweeps it.
            _log.exception("commission failed after dispatch returned: %s", exc)


async def start(
    config: Config,
    draft_id: str,
    *,
    gateway_cm: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
) -> RunRecord:
    """Begin a commission and return as soon as it has a run id.

    Raises whatever preparation raises — an over-ceiling estimate or a missing
    draft still fails loudly and immediately, because nothing has been spent.

    ``gateway_cm`` exists so tests exercise this function rather than a copy of
    it; production callers omit it and get the real OpenRouter gateway.
    """
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[RunRecord] = loop.create_future()
    task = asyncio.create_task(_execute(config, ready, draft_id, gateway_cm or _live_gateway))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)

    run = await ready
    task.set_name(f"dispatch:{run.run_id}")
    return run


def describe(config: Config, run_id: str) -> str:
    """One line on whether this process is still working on a run."""
    if run_id in in_flight_run_ids():
        return "running here now"
    record = RunStore(config.data_dir).load_run(run_id)
    if record.status == "running":
        return "marked running, but not by this process — it may be another server, or interrupted"
    return record.status
