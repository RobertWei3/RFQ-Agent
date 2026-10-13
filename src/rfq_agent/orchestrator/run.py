"""Drive a run (spec 13): batches in order, then finalize; start, resume, rerun-failed."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command, Interrupt

from rfq_agent.agents.runner import McpInternalDB, mcp_internal_session
from rfq_agent.config import Settings
from rfq_agent.orchestrator.batch import BatchGraph, BatchState, batch_input, build_batch_graph
from rfq_agent.orchestrator.context import AgentDeps, RunContext
from rfq_agent.orchestrator.finalize import FinalGraph, FinalState, build_final_graph, final_input
from rfq_agent.orchestrator.lease import hold_lease, new_lease_owner
from rfq_agent.orchestrator.prices import PricePlan
from rfq_agent.schemas import (
    OPEN_BATCH_STATUSES,
    BatchRecord,
    CostBreakdown,
    Evidence,
    MSRPResult,
    OECandidate,
    OEResolution,
    PriceQuote,
    RetailResult,
    ReviewDecision,
    ReviewInput,
    ReviewPrompt,
    ReviewQueue,
    RFQLine,
    RowFailure,
    RunRecord,
    RunSpec,
    RunStatus,
    ShouldCostResult,
)
from rfq_agent.store.db import BUSY_TIMEOUT_MS, open_sqlite
from rfq_agent.store.runs import RunStore, open_store
from rfq_agent.tools import build_tools
from rfq_agent.trace import TraceWriter

__all__ = [
    "AgentDeps",
    "NotInReviewQueue",
    "ReviewNotOpen",
    "RunOptions",
    "RunReport",
    "Reviewer",
    "answer_review",
    "ensure_reviewable",
    "rerun_failed",
    "resume_run",
    "review_queue",
    "start_run",
]

# Types that appear in checkpointed state; registering them keeps deserialization allowed.
CHECKPOINT_TYPES: tuple[type, ...] = (
    RFQLine,
    OEResolution,
    OECandidate,
    Evidence,
    MSRPResult,
    PriceQuote,
    RetailResult,
    ShouldCostResult,
    CostBreakdown,
    RowFailure,
    PricePlan,
)

_Input = TypeVar("_Input", BatchState, FinalState)

# Asked about the next queued red row; returning None pauses the review (resume later).
Reviewer = Callable[[ReviewPrompt], Awaitable[ReviewInput | None]]


class ReviewNotOpen(RuntimeError):
    """The run takes no review now: review is off, or it is not awaiting review."""


class NotInReviewQueue(RuntimeError):
    """The row is not waiting for review (already reviewed, or not red)."""


@dataclass
class RunOptions:
    """How this process runs: injected agent dependencies, reviewer, local trace file."""

    deps: AgentDeps | None = None  # None: mock tools + an MCP session to the internal DB
    reviewer: Reviewer | None = None  # None: a review stays paused
    trace_path: Path | None = None
    lease_token: str | None = None  # lease handed over by the API; None: take it ourselves
    # Called once the run is finalized, before the reviewer is asked anything.
    on_finalized: Callable[[], object] | None = None


@dataclass
class RunReport:
    run_id: str
    status: RunStatus
    batches: list[BatchRecord]


def batch_thread(run_id: str, batch_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": f"{run_id}:{batch_id}"}}


def final_thread(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": f"{run_id}:final"}}


async def start_run(
    settings: Settings, spec: RunSpec, lines: Sequence[RFQLine], options: RunOptions | None = None
) -> RunReport:
    """Create a run and process it to the end (or until review is paused)."""
    with open_store(settings.db_path) as store:
        run_id = store.create_run(spec, lines)
    return await resume_run(settings, run_id, options)


async def resume_run(
    settings: Settings, run_id: str, options: RunOptions | None = None
) -> RunReport:
    """Skip completed batches, continue interrupted ones, then finalize (spec 13)."""
    opts = options or RunOptions()
    async with _session(settings, run_id, opts) as session:
        await session.run_open_batches()
        await session.finalize(opts, resume=True)
        return session.report()


async def rerun_failed(
    settings: Settings, run_id: str, options: RunOptions | None = None
) -> RunReport:
    """Rerun failed batches and rows with a failure, then finalize again (spec 13).

    The plan is saved before anything runs, so a resume after a crash finishes all of it.
    """
    opts = options or RunOptions()
    async with _session(settings, run_id, opts) as session:
        plan = session.store.rows_to_rerun(run_id)
        # Old checkpoints go first: a crash before the plan is saved then leaves nothing
        # stale behind. Finalize starts over too (a paused review queued pre-rerun rows).
        for batch_id in plan:
            await _restart(session.batch_graph, batch_thread(run_id, batch_id))
        await _restart(session.final_graph, final_thread(run_id))
        session.store.reopen_for_rerun(run_id, plan)
        await session.run_open_batches()
        await session.finalize(opts, resume=False)
        return session.report()


async def review_queue(settings: Settings, run_id: str) -> list[int]:
    """Rows the run's paused review is waiting on; empty when no review is pending."""
    async with open_checkpointer(settings.checkpoint_path) as saver:
        state = await build_final_graph(saver).aget_state(final_thread(run_id))
    return _queued(state.interrupts)


async def answer_review(
    settings: Settings, run_id: str, decision: ReviewDecision, options: RunOptions | None = None
) -> RunReport:
    """Record one decision for any queued row, then summarize the run again (spec 13).

    Raises ``ReviewNotOpen`` or ``NotInReviewQueue`` before anything changes, and
    ``LeaseHeld`` if another process drives the run.
    """
    with open_store(settings.db_path) as store:
        record = store.run(run_id)
    ensure_reviewable(record, None, decision.row_id)
    opts = options or RunOptions()
    async with _session(settings, run_id, opts) as session:
        config = final_thread(run_id)
        queued = _queued((await session.final_graph.aget_state(config)).interrupts)
        ensure_reviewable(record, queued, decision.row_id)
        await session.resume_final(decision)
        return session.report()


def ensure_reviewable(record: RunRecord, queue: Sequence[int] | None, row_id: int) -> None:
    """Raise unless the run takes reviews now and ``row_id`` is queued (``None``: skip that).

    ``ReviewNotOpen``: review is off or the run is not awaiting review;
    ``NotInReviewQueue``: the row was reviewed already or is not red.
    """
    if not record.spec.review or record.status != "awaiting_review":
        raise ReviewNotOpen(f"run {record.run_id} is {record.status} (review={record.spec.review})")
    if queue is not None and row_id not in queue:
        raise NotInReviewQueue(f"row {row_id} of run {record.run_id} is not waiting for review")


def _queued(interrupts: Sequence[Interrupt]) -> list[int]:
    return ReviewQueue.model_validate(interrupts[0].value).row_ids if interrupts else []


@dataclass
class _Session:
    """One process working on one run: store, compiled graphs and their context."""

    run_id: str
    store: RunStore
    ctx: RunContext
    batch_graph: BatchGraph
    final_graph: FinalGraph

    async def run_open_batches(self) -> None:
        for batch in self.store.batches(self.run_id):
            if batch.status in OPEN_BATCH_STATUSES:
                await self.run_batch(batch)

    async def run_batch(self, batch: BatchRecord) -> None:
        """Continue the batch's interrupted thread, or start it on its rows to run."""
        config = batch_thread(self.run_id, batch.batch_id)
        rows = batch.rows_to_run()
        manual = {
            o.row_id: o.oe
            for o in self.store.outcomes(self.run_id, rows)
            if o.manual_oe and isinstance(o.oe, OEResolution)
        }
        fresh = batch_input(self.run_id, batch.batch_id, rows, manual)
        payload = await _continue_or_restart(self.batch_graph, config, fresh, resume=True)
        self.store.start_batch(self.run_id, batch.batch_id)
        try:
            await self.batch_graph.ainvoke(
                payload, config=config, context=self.ctx, durability="sync"
            )
        except Exception as exc:  # noqa: BLE001 - a broken batch must not stop the run
            self.store.fail_batch(self.run_id, batch.batch_id, f"{type(exc).__name__}: {exc}")

    async def finalize(self, options: RunOptions, *, resume: bool) -> None:
        config = final_thread(self.run_id)
        finished = self.store.run(self.run_id).status == "completed"
        if resume and finished and not (await self.final_graph.aget_state(config)).next:
            return
        fresh = final_input(self.run_id)
        payload = await _continue_or_restart(self.final_graph, config, fresh, resume=resume)
        result = await self.final_graph.ainvoke(
            payload, config=config, context=self.ctx, durability="sync"
        )
        reviewer = options.reviewer
        if reviewer is not None and result.get("__interrupt__") and options.on_finalized:
            options.on_finalized()
        while reviewer is not None and (queue := _queued(result.get("__interrupt__", []))):
            row = self.store.rows(self.run_id, [queue[0]])[0]
            answer = await reviewer(ReviewPrompt(run_id=self.run_id, remaining=len(queue), row=row))
            if answer is None:
                return
            result = await self.resume_final(ReviewDecision.for_row(queue[0], answer))

    async def resume_final(self, decision: ReviewDecision) -> dict[str, Any]:
        """Answer the paused review; returns once it pauses again or the run completes."""
        return await self.final_graph.ainvoke(
            Command(resume=decision.model_dump(mode="json")),
            config=final_thread(self.run_id),
            context=self.ctx,
            durability="sync",
        )

    def report(self) -> RunReport:
        return RunReport(
            run_id=self.run_id,
            status=self.store.run(self.run_id).status,
            batches=self.store.batches(self.run_id),
        )


async def _continue_or_restart(
    graph: BatchGraph | FinalGraph, config: RunnableConfig, fresh: _Input, *, resume: bool
) -> _Input | None:
    """``None`` continues a thread that stopped midway; otherwise restart it on ``fresh``."""
    if resume and (await graph.aget_state(config)).next:
        return None
    await _restart(graph, config)
    return fresh


async def _restart(graph: BatchGraph | FinalGraph, config: RunnableConfig) -> None:
    """New input restarts a thread; drop its old state first so reducers start empty."""
    saver = graph.checkpointer
    if not isinstance(saver, BaseCheckpointSaver):
        raise TypeError(f"orchestrator graphs need a checkpoint saver, got {saver!r}")
    await saver.adelete_thread(config["configurable"]["thread_id"])


@asynccontextmanager
async def _session(settings: Settings, run_id: str, options: RunOptions) -> AsyncIterator[_Session]:
    async with AsyncExitStack() as stack:
        store = stack.enter_context(open_store(settings.db_path))
        spec = store.run(run_id).spec
        ttl_s, heartbeat_s = settings.storage.lease_ttl_s, settings.storage.lease_heartbeat_s
        await stack.enter_async_context(
            hold_lease(
                store,
                run_id,
                new_lease_owner(),
                ttl_s=ttl_s,
                heartbeat_s=heartbeat_s,
                handed_over=options.lease_token,
            )
        )
        dataset_dir = Path(spec.dataset_dir)
        deps = options.deps
        if deps is None:
            deps = await stack.enter_async_context(open_deps(settings, dataset_dir))
        settings = settings.model_copy(
            update={
                "pipeline": settings.pipeline.model_copy(update={"batch_size": spec.batch_size})
            }
        )
        path = options.trace_path
        trace = TraceWriter(path, run_id, append=True) if path else None
        saver = await stack.enter_async_context(open_checkpointer(settings.checkpoint_path))
        ctx = RunContext.build(
            run_id, settings, store, deps, strategy=spec.strategy, review=spec.review, trace=trace
        )
        yield _Session(
            run_id=run_id,
            store=store,
            ctx=ctx,
            batch_graph=build_batch_graph(saver),
            final_graph=build_final_graph(saver),
        )


@asynccontextmanager
async def open_checkpointer(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    """SQLite checkpointer in its own file, with the state's Pydantic types registered."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # What aiosqlite.connect() builds, with our opener so opens and closes go through
    # OPEN_CLOSE_LOCK. aiosqlite.Connection(connector, iter_chunk_size) is not in its docs;
    # 64 is aiosqlite.connect()'s own default. Recheck when upgrading aiosqlite.
    connector = partial(open_sqlite, path, timeout=BUSY_TIMEOUT_MS / 1000)
    async with aiosqlite.Connection(connector, iter_chunk_size=64) as conn:
        # Shared with other processes; the saver's setup switches the file to WAL.
        await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
        yield AsyncSqliteSaver(conn, serde=serde)


@asynccontextmanager
async def open_deps(settings: Settings, dataset_dir: Path) -> AsyncIterator[AgentDeps]:
    """Mock tools plus one MCP session to the internal DB, shared by every batch."""
    async with mcp_internal_session(settings, dataset_dir) as call:

        async def lookup(customer_part_no: str) -> str | None:
            data = await call("lookup_our_part", {"customer_part_no": customer_part_no})
            return data["our_part_no"] if data.get("found") else None

        yield AgentDeps(
            lookup=lookup,
            internal_db=McpInternalDB(call),
            tools=build_tools(dataset_dir, settings.tools),
        )
