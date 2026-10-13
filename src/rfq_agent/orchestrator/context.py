"""Runtime context of the orchestrator graphs (never checkpointed; rebuilt on every call)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict

from rfq_agent.agents.oe_interchange import OEContext
from rfq_agent.agents.oe_msrp import MSRPContext
from rfq_agent.agents.retail_price import RetailContext
from rfq_agent.agents.runner import Lookup
from rfq_agent.agents.should_cost import InternalDB, ShouldCostContext
from rfq_agent.config import Settings
from rfq_agent.schemas import StrategyName
from rfq_agent.store.runs import RunStore
from rfq_agent.tools.base import ToolProvider
from rfq_agent.trace import TraceWriter


@dataclass
class AgentDeps:
    """External dependencies of the agents: internal DB (MCP) and the mock tools."""

    lookup: Lookup
    internal_db: InternalDB
    tools: Mapping[str, ToolProvider]


class _AgentKwargs(TypedDict):
    """Fields every agent context gets from the run."""

    tools: Mapping[str, ToolProvider]
    settings: Settings
    run_id: str
    trace: TraceWriter | None


@dataclass
class RunContext:
    """Everything the batch and finalize graph nodes need for one run."""

    run_id: str
    settings: Settings
    store: RunStore
    strategy: StrategyName
    review: bool
    oe: OEContext
    msrp: MSRPContext
    retail: RetailContext
    cost: ShouldCostContext
    trace: TraceWriter | None = None

    @classmethod
    def build(
        cls,
        run_id: str,
        settings: Settings,
        store: RunStore,
        deps: AgentDeps,
        *,
        strategy: StrategyName,
        review: bool,
        trace: TraceWriter | None,
    ) -> RunContext:
        common = _AgentKwargs(tools=deps.tools, settings=settings, run_id=run_id, trace=trace)
        return cls(
            run_id=run_id,
            settings=settings,
            store=store,
            strategy=strategy,
            review=review,
            oe=OEContext(lookup_our_part=deps.lookup, **common),
            msrp=MSRPContext(**common),
            retail=RetailContext(**common),
            cost=ShouldCostContext(internal_db=deps.internal_db, **common),
            trace=trace,
        )
