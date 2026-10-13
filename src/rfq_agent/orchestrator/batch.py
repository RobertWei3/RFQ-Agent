"""Batch graph (spec 13): the four agents over one batch, fanned out per row with Send."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Checkpointer, Send

from rfq_agent.agents.oe_interchange import resolve_line
from rfq_agent.agents.oe_msrp import resolve_msrp
from rfq_agent.agents.retail_price import resolve_retail
from rfq_agent.agents.should_cost import resolve_should_cost
from rfq_agent.orchestrator.context import RunContext
from rfq_agent.orchestrator.prices import (
    PricePlan,
    expand_price_results,
    plan_price_jobs,
    price_key,
)
from rfq_agent.schemas import (
    MSRPResult,
    OEResolution,
    RetailResult,
    RFQLine,
    RowFailure,
    ShouldCostResult,
)
from rfq_agent.store.runs import RowOutcomes

OEOutcome = OEResolution | RowFailure
MSRPOutcome = MSRPResult | RowFailure
RetailOutcome = RetailResult | RowFailure
CostOutcome = ShouldCostResult | RowFailure


def _merge(left: dict[int, Any], right: dict[int, Any]) -> dict[int, Any]:
    return {**left, **right}


class BatchState(TypedDict, total=False):
    """State of one batch: its lines and the per-row results of every stage (spec 13)."""

    run_id: str
    batch_id: int
    row_ids: list[int]
    manual_oe: dict[int, OEResolution]  # OE entered in review: the OE agent is skipped
    lines: list[RFQLine]
    oe: Annotated[dict[int, OEOutcome], _merge]
    msrp: Annotated[dict[int, MSRPOutcome], _merge]
    retail: Annotated[dict[int, RetailOutcome], _merge]
    should_cost: Annotated[dict[int, CostOutcome], _merge]
    msrp_plan: PricePlan[MSRPResult]
    retail_plan: PricePlan[RetailResult]
    errors: Annotated[list[str], operator.add]


class OERowInput(TypedDict):
    line: RFQLine


class PriceRowInput(TypedDict):
    upstream: OEOutcome


class CostRowInput(TypedDict):
    line: RFQLine
    upstream: OEOutcome


def batch_input(
    run_id: str,
    batch_id: int,
    row_ids: list[int],
    manual_oe: dict[int, OEResolution] | None = None,
) -> BatchState:
    return {
        "run_id": run_id,
        "batch_id": batch_id,
        "row_ids": row_ids,
        "manual_oe": manual_oe or {},
    }


async def load_batch(state: BatchState, runtime: Runtime[RunContext]) -> BatchState:
    return {"lines": runtime.context.store.lines(state["run_id"], state["row_ids"])}


def route_oe(state: BatchState) -> list[Send] | str:
    manual = state.get("manual_oe", {})
    sends = [Send("oe_row", {"line": x}) for x in state["lines"] if x.row_id not in manual]
    return sends or "plan_prices"


async def oe_row(state: OERowInput, runtime: Runtime[RunContext]) -> BatchState:
    line = state["line"]
    return {"oe": {line.row_id: await resolve_line(line, runtime.context.oe)}}


async def plan_prices(state: BatchState, runtime: Runtime[RunContext]) -> BatchState:
    """Decide which rows run the price agents and which reuse a result (spec 13)."""
    upstream = _upstream(state)
    keys = {k for k in (price_key(o) for o in upstream.values()) if k is not None}
    cached = runtime.context.store.cached_prices(state["run_id"], keys)
    return {
        "oe": dict(state.get("manual_oe", {})),
        "msrp_plan": plan_price_jobs(list(upstream.values()), cached.msrp),
        "retail_plan": plan_price_jobs(list(upstream.values()), cached.retail),
    }


def route_downstream(state: BatchState) -> list[Send]:
    upstream = _upstream(state)
    return [
        *(Send("msrp_row", {"upstream": o}) for o in state["msrp_plan"].jobs),
        *(Send("retail_row", {"upstream": o}) for o in state["retail_plan"].jobs),
        *(
            Send("should_cost_row", {"line": x, "upstream": upstream[x.row_id]})
            for x in state["lines"]
        ),
    ]


async def msrp_row(state: PriceRowInput, runtime: Runtime[RunContext]) -> BatchState:
    upstream = state["upstream"]
    return {"msrp": {upstream.row_id: await resolve_msrp(upstream, runtime.context.msrp)}}


async def retail_row(state: PriceRowInput, runtime: Runtime[RunContext]) -> BatchState:
    upstream = state["upstream"]
    return {"retail": {upstream.row_id: await resolve_retail(upstream, runtime.context.retail)}}


async def should_cost_row(state: CostRowInput, runtime: Runtime[RunContext]) -> BatchState:
    line = state["line"]
    result = await resolve_should_cost(line, state["upstream"], runtime.context.cost)
    return {"should_cost": {line.row_id: result}}


async def collect(state: BatchState) -> BatchState:
    """Give followers their leader's price results and reused rows the cached ones."""
    msrp = expand_price_results(state["msrp_plan"], list(state.get("msrp", {}).values()))
    retail = expand_price_results(state["retail_plan"], list(state.get("retail", {}).values()))
    outcomes = _row_outcomes(state, msrp, retail)
    return {
        "msrp": msrp,
        "retail": retail,
        "errors": [error for row in outcomes for error in _errors(row)],
    }


async def persist_batch(state: BatchState, runtime: Runtime[RunContext]) -> BatchState:
    """Write the four outcomes of every row and mark the batch completed (one transaction)."""
    outcomes = _row_outcomes(state, state["msrp"], state["retail"])
    runtime.context.store.save_batch(state["run_id"], state["batch_id"], outcomes)
    return {}


def _upstream(state: BatchState) -> dict[int, OEOutcome]:
    return {**state.get("oe", {}), **state.get("manual_oe", {})}


def _row_outcomes(
    state: BatchState, msrp: dict[int, MSRPOutcome], retail: dict[int, RetailOutcome]
) -> list[RowOutcomes]:
    upstream = _upstream(state)
    manual = state.get("manual_oe", {})
    return [
        RowOutcomes(
            row_id=x.row_id,
            oe=upstream[x.row_id],
            msrp=msrp[x.row_id],
            retail=retail[x.row_id],
            should_cost=state["should_cost"][x.row_id],
            manual_oe=x.row_id in manual,
            msrp_from=state["msrp_plan"].copied_from(x.row_id),
            retail_from=state["retail_plan"].copied_from(x.row_id),
        )
        for x in state["lines"]
    ]


def _errors(row: RowOutcomes) -> list[str]:
    paths = {"oe": row.oe, "msrp": row.msrp, "retail": row.retail, "cost": row.should_cost}
    return [
        f"{row.row_id}:{path}:{o.stage}:{o.error}"
        for path, o in paths.items()
        if isinstance(o, RowFailure)
    ]


BatchGraph = CompiledStateGraph[BatchState, RunContext, BatchState, BatchState]


def build_batch_graph(checkpointer: Checkpointer) -> BatchGraph:
    graph = StateGraph(BatchState, context_schema=RunContext)
    graph.add_node("load_batch", load_batch)
    graph.add_node("oe_row", oe_row, input_schema=OERowInput)
    graph.add_node("plan_prices", plan_prices)
    graph.add_node("msrp_row", msrp_row, input_schema=PriceRowInput)
    graph.add_node("retail_row", retail_row, input_schema=PriceRowInput)
    graph.add_node("should_cost_row", should_cost_row, input_schema=CostRowInput)
    graph.add_node("collect", collect)
    graph.add_node("persist_batch", persist_batch)
    graph.add_edge(START, "load_batch")
    graph.add_conditional_edges("load_batch", route_oe, ["oe_row", "plan_prices"])
    graph.add_edge("oe_row", "plan_prices")
    graph.add_conditional_edges(
        "plan_prices", route_downstream, ["msrp_row", "retail_row", "should_cost_row"]
    )
    for node in ("msrp_row", "retail_row", "should_cost_row"):
        graph.add_edge(node, "collect")
    graph.add_edge("collect", "persist_batch")
    graph.add_edge("persist_batch", END)
    return graph.compile(checkpointer=checkpointer)
