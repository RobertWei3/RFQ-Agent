"""Finalize graph (spec 13): resolution, confidence and review over the whole run."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Checkpointer, Send, interrupt

from rfq_agent.confidence import score_rows
from rfq_agent.orchestrator.context import RunContext
from rfq_agent.orchestrator.review import apply_reviews, record_decision
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import (
    ConfidenceResult,
    MSRPResult,
    OEResolution,
    ResolvedRow,
    RetailResult,
    ReviewDecision,
    ReviewEntry,
    ReviewQueue,
    RFQLine,
    RowFailure,
    ShouldCostResult,
)
from rfq_agent.store.runs import RowOutcomes, RunStore, StoredRow

BATCH_FAILED_STAGE = "batch"


class FinalState(TypedDict, total=False):
    run_id: str
    to_score: list[int]
    queue: list[int]  # red rows still waiting for review, by row_id


class ScoreChunkInput(TypedDict):
    row_ids: list[int]


def final_input(run_id: str) -> FinalState:
    return {"run_id": run_id}


async def resolve(state: FinalState, runtime: Runtime[RunContext]) -> FinalState:
    """Merge the whole run; rows whose resolved content did not change keep their score.

    Rows to score are stored without review overlays: confidence is about the system's
    own conclusions, so a manual price never reaches it (spec 13).
    """
    ctx = runtime.context
    run_id = state["run_id"]
    resolved = resolve_rows(*_stage_inputs(ctx.store, run_id))
    previous = {s.row.line.row_id: s for s in ctx.store.stored_rows(run_id)}
    reviews = reviews_by_row(ctx.store.reviews(run_id))
    rows: list[StoredRow] = []
    to_score: list[int] = []
    for row in resolved:
        rid = row.line.row_id
        basis = _fingerprint(row)
        old = previous.get(rid)
        if old is not None and old.basis == basis and old.row.confidence is not None:
            scored = _with_score(row, old.row.confidence, old.extra_reasons)
            shown = apply_reviews(scored, reviews.get(rid, []))
            rows.append(StoredRow(row=shown, basis=basis, extra_reasons=old.extra_reasons))
        else:
            rows.append(StoredRow(row=row, basis=basis))
            to_score.append(rid)
    ctx.store.save_rows(run_id, rows)
    return {"to_score": to_score}


def route_scoring(state: FinalState, runtime: Runtime[RunContext]) -> list[Send] | str:
    size = runtime.context.settings.pipeline.batch_size
    ids = state["to_score"]
    sends = [Send("score_chunk", {"row_ids": ids[i : i + size]}) for i in range(0, len(ids), size)]
    return sends or "review_gate"


async def score_chunk(state: ScoreChunkInput, runtime: Runtime[RunContext]) -> FinalState:
    """Score rows stored by ``resolve`` (no overlays yet), then apply review decisions."""
    ctx = runtime.context
    stored = ctx.store.stored_rows(ctx.run_id, state["row_ids"])
    scored = await score_rows(
        [s.row for s in stored], ctx.settings, ctx.strategy, run_id=ctx.run_id, trace=ctx.trace
    )
    reviews = reviews_by_row(ctx.store.reviews(ctx.run_id))
    ctx.store.save_rows(
        ctx.run_id,
        [
            StoredRow(
                row=apply_reviews(new, reviews.get(new.line.row_id, [])),
                basis=old.basis,
                extra_reasons=new.worksheet.review_reasons[len(old.row.worksheet.review_reasons) :],
            )
            for old, new in zip(stored, scored, strict=True)
        ],
    )
    return {}


async def review_gate(state: FinalState, runtime: Runtime[RunContext]) -> FinalState:
    """Queue red rows nobody has reviewed yet (only in review mode).

    The run is finalized either way: with rows queued it awaits review, the worksheet
    already downloadable (spec 13).
    """
    ctx = runtime.context
    if not ctx.review:
        return {"queue": []}
    reviewed = {e.row_id for e in ctx.store.reviews(state["run_id"])}
    queue = [
        r.line.row_id
        for r in ctx.store.rows(state["run_id"])
        if r.worksheet.band == "red" and r.line.row_id not in reviewed
    ]
    if queue:
        ctx.store.mark_finalized(state["run_id"], "awaiting_review")
    return {"queue": queue}


def route_review(state: FinalState) -> str:
    return "review" if state["queue"] else "finish"


async def review(state: FinalState, runtime: Runtime[RunContext]) -> FinalState:
    """Put the whole queue to review (``interrupt``) and record one row's answer (spec 13).

    The answer may be for any queued row; one for a row no longer queued is ignored. Then
    the run is summarized again (``resolve``) and the remaining queue put up again.

    On resume the node reruns from the top; recording is skipped when the row already has
    a review entry, so a crash after recording does not log or rerun agents twice.
    """
    ctx = runtime.context
    queue = state["queue"]
    answer = interrupt(ReviewQueue(run_id=state["run_id"], row_ids=queue).model_dump(mode="json"))
    decision = ReviewDecision.model_validate(answer)
    if decision.row_id in queue:
        row = ctx.store.rows(state["run_id"], [decision.row_id])[0]
        await record_decision(ctx, row, decision)
    return {}


async def finish(state: FinalState, runtime: Runtime[RunContext]) -> FinalState:
    runtime.context.store.mark_finalized(state["run_id"], "completed")
    return {}


def reviews_by_row(entries: list[ReviewEntry]) -> dict[int, list[ReviewEntry]]:
    grouped: dict[int, list[ReviewEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.row_id].append(entry)
    return grouped


StageInputs = tuple[
    list[RFQLine],
    list[OEResolution | RowFailure],
    list[MSRPResult | RowFailure],
    list[RetailResult | RowFailure],
    list[ShouldCostResult | RowFailure],
]


def _stage_inputs(store: RunStore, run_id: str) -> StageInputs:
    """Lines and the four outcome lists; rows of a failed batch fail on every path."""
    lines = store.lines(run_id)
    stored = {o.row_id: o for o in store.outcomes(run_id)}
    errors = {
        rid: b.error or "batch failed"
        for b in store.batches(run_id)
        if b.status == "failed"
        for rid in b.row_ids
    }
    outcomes = [stored.get(x.row_id) or _batch_failure(x.row_id, errors) for x in lines]
    return (
        lines,
        [o.oe for o in outcomes],
        [o.msrp for o in outcomes],
        [o.retail for o in outcomes],
        [o.should_cost for o in outcomes],
    )


def _batch_failure(row_id: int, errors: dict[int, str]) -> RowOutcomes:
    failure = RowFailure(
        row_id=row_id, stage=BATCH_FAILED_STAGE, error=errors.get(row_id, "batch not run")
    )
    return RowOutcomes(row_id=row_id, oe=failure, msrp=failure, retail=failure, should_cost=failure)


def _fingerprint(row: ResolvedRow) -> str:
    return hashlib.sha256(row.model_dump_json().encode("utf-8")).hexdigest()


def _with_score(row: ResolvedRow, confidence: ConfidenceResult, extra: list[str]) -> ResolvedRow:
    """``row`` with a score kept from an earlier finalize, as ``score_rows`` would set it."""
    worksheet = row.worksheet.model_copy(
        update={
            "confidence_score": confidence.score,
            "band": confidence.band,
            "review_reasons": [*row.worksheet.review_reasons, *extra],
        }
    )
    return row.model_copy(update={"worksheet": worksheet, "confidence": confidence})


FinalGraph = CompiledStateGraph[FinalState, RunContext, FinalState, FinalState]


def build_final_graph(checkpointer: Checkpointer) -> FinalGraph:
    graph = StateGraph(FinalState, context_schema=RunContext)
    graph.add_node("resolve", resolve)
    graph.add_node("score_chunk", score_chunk, input_schema=ScoreChunkInput)
    graph.add_node("review_gate", review_gate)
    graph.add_node("review", review)
    graph.add_node("finish", finish)
    graph.add_edge(START, "resolve")
    graph.add_conditional_edges("resolve", route_scoring, ["score_chunk", "review_gate"])
    graph.add_edge("score_chunk", "review_gate")
    graph.add_conditional_edges("review_gate", route_review, ["review", "finish"])
    # Every answer re-summarizes: overlays and manual OEs show up, changed rows are
    # rescored and any newly red row is queued.
    graph.add_edge("review", "resolve")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)
