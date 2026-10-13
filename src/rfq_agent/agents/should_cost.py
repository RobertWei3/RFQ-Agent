"""``should_cost`` agent (spec section 11.4): our part's spec -> full cost stack.

A per-row LangGraph subgraph:

    prepare -> internal_spec -> search_external -> extract_spec -> estimate

The internal record (via MCP) is the primary source. When the OE is resolved, the
external spec page for ``current_oe`` is read to cross-check or fill gaps; for an
unknown part the OEM catalog page also supplies the category. One LLM call
extracts page facts; deterministic code (``should_cost_rules``) grounds them,
reconciles them with the internal record and computes every cost term.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel

from rfq_agent.agents.common import (
    AgentContext,
    RowFailure,
    check_upstream,
    ok_or_end,
    record_failure,
    render_pages,
    run_row,
    search_with_retry,
    split_chain_pages,
)
from rfq_agent.agents.should_cost_rules import (
    OTHER_MATERIAL,
    ExternalField,
    ExternalSpec,
    PageKind,
    ReconciledSpec,
    SpecPage,
    SpecPageFacts,
    category_from_text,
    reconcile_spec,
    verify_spec_pages,
)
from rfq_agent.costing.formula import compute_breakdown
from rfq_agent.schemas import (
    CostBreakdown,
    Evidence,
    MaterialPrice,
    OEResolution,
    PartSpec,
    RFQLine,
    ShouldCostResult,
    SpecSource,
)
from rfq_agent.tools.base import ToolHit

AGENT = "should_cost"
INTERNAL_DB_SOURCE = "internal_db"
SPEC_TOOL = "external_spec_search"
CATALOG_TOOL = "oem_catalog_search"


class InternalDB(Protocol):
    """The internal-database calls should_cost needs (served by the MCP server)."""

    async def get_part_spec(self, our_part_no: str) -> PartSpec: ...

    async def list_materials(self) -> list[str]: ...

    async def get_material_price(self, material: str) -> MaterialPrice: ...


# --- LLM extraction schema (internal) -------------------------------------------------


class SpecExtraction(BaseModel):
    """Spec facts for every page in the search results."""

    pages: list[SpecPageFacts]
    reasoning: str


SPEC_SYSTEM = """You read product data sheets and OEM catalog pages for an aftermarket \
auto-parts RFQ. For every page report: the material phrase exactly as written \
(material_text) and which of the known material keys it means (material_key; "other" if \
none fits); the weight number and unit exactly as written (weight_value, weight_unit), \
without converting units; and the product type phrase exactly as written (category_text, \
e.g. the Description line of a catalog page). Use null for anything the page does not \
show. Never invent values. Search results may include pages about unrelated parts; still \
report them."""


# --- context, state -------------------------------------------------------------------


@dataclass(kw_only=True)
class ShouldCostContext(AgentContext):
    internal_db: InternalDB
    agent: str = AGENT


@dataclass(frozen=True)
class ShouldCostInput:
    """One row for should_cost: the RFQ line and its oe_interchange outcome."""

    line: RFQLine
    upstream: OEResolution | RowFailure


class ShouldCostState(TypedDict, total=False):
    line: RFQLine
    upstream: OEResolution | RowFailure
    current_oe: str | None  # set only for a resolved OE
    part: PartSpec | None  # internal record; None when our part is unknown
    known_materials: list[str]
    hits: dict[str, ToolHit]
    pages: dict[str, SpecPage]
    extraction: SpecExtraction | None
    notes: list[str]  # reasoning gathered before the estimate
    failure: RowFailure | None
    result: ShouldCostResult | None


def _needs_external(state: ShouldCostState) -> bool:
    """True if the external pages fill gaps (a failure there is a row failure)."""
    part = state.get("part")
    return part is None or None in (part.category, part.material, part.weight_kg)


# --- nodes ----------------------------------------------------------------------------


async def prepare(state: ShouldCostState, runtime: Runtime[ShouldCostContext]) -> ShouldCostState:
    ctx, line, upstream = runtime.context, state["line"], state["upstream"]
    ctx.emit(line.row_id, "start", line=line, upstream=upstream)
    check = check_upstream(upstream)
    if check.failure is not None:
        ctx.emit(line.row_id, "failure", stage=check.failure.stage, error=check.failure.error)
        return {"failure": check.failure}
    assert isinstance(upstream, OEResolution)
    notes = []
    if check.skip_reason is not None:
        notes.append(f"no external spec lookup: {check.skip_reason}")
    current_oe = upstream.current_oe if check.skip_reason is None else None
    ctx.emit(line.row_id, "prepare", current_oe=current_oe)
    return {"current_oe": current_oe, "notes": notes}


async def internal_spec(
    state: ShouldCostState, runtime: Runtime[ShouldCostContext]
) -> ShouldCostState:
    ctx, line, upstream = runtime.context, state["line"], state["upstream"]
    assert isinstance(upstream, OEResolution)
    our_part_no = line.our_part_no or upstream.our_part_no
    notes = list(state["notes"])
    try:
        known_materials = await ctx.internal_db.list_materials()
        part = await ctx.internal_db.get_part_spec(our_part_no) if our_part_no else None
    except Exception as exc:  # noqa: BLE001
        return {"failure": record_failure(ctx, line.row_id, "internal_spec", exc)}
    if our_part_no is None:
        notes.append("our part number unknown; costing from external pages only")
    elif part is None or not part.found:
        notes.append(f"our part {our_part_no} not in the internal DB; external pages only")
        part = None
    ctx.emit(line.row_id, "internal_spec", our_part_no=our_part_no, part=part)
    return {"part": part, "known_materials": known_materials, "notes": notes}


async def search_external(
    state: ShouldCostState, runtime: Runtime[ShouldCostContext]
) -> ShouldCostState:
    ctx, row_id, current_oe = runtime.context, state["line"].row_id, state.get("current_oe")
    if current_oe is None:
        return {"hits": {}, "pages": {}}
    kinds: dict[str, PageKind] = {SPEC_TOOL: "spec"}
    part = state.get("part")
    if part is None or part.category is None:  # the catalog page names the category
        kinds[CATALOG_TOOL] = "catalog"
    try:
        results = await asyncio.gather(
            *(search_with_retry(ctx.tools[tool], current_oe) for tool in kinds)
        )
    except Exception as exc:  # noqa: BLE001
        return _external_failed(state, ctx, "search_spec", exc)
    hits: dict[str, ToolHit] = {}
    pages: dict[str, SpecPage] = {}
    filtered: list[str] = []
    for kind, result in zip(kinds.values(), results, strict=True):
        kept, decoys = split_chain_pages(result, [current_oe])
        filtered.extend(decoys)
        for hit in kept:
            hits[hit.page_id] = hit
            pages[hit.page_id] = SpecPage(kind=kind, text=hit.text)
    ctx.emit(row_id, "search_external", pages_kept=sorted(pages), pages_filtered_out=filtered)
    return {"hits": hits, "pages": pages}


async def extract_spec(
    state: ShouldCostState, runtime: Runtime[ShouldCostContext]
) -> ShouldCostState:
    ctx, row_id, hits = runtime.context, state["line"].row_id, state.get("hits", {})
    if not hits:
        return {"extraction": None}
    messages: list[BaseMessage] = [
        SystemMessage(content=SPEC_SYSTEM),
        HumanMessage(
            content=f"Current OE number: {state['current_oe']}\n"
            f"Known material keys: {', '.join(state['known_materials'])}\n\n"
            f"{render_pages(list(hits.values()))}"
        ),
    ]
    try:
        extraction = await ctx.extract(SpecExtraction, messages, row_id)
    except Exception as exc:  # noqa: BLE001
        return _external_failed(state, ctx, "extract_spec", exc)
    return {"extraction": extraction}


def _external_failed(
    state: ShouldCostState, ctx: ShouldCostContext, stage: str, exc: BaseException
) -> ShouldCostState:
    """A failed lookup sinks the row only when the external pages were needed to fill gaps."""
    row_id = state["line"].row_id
    if _needs_external(state):
        return {"failure": record_failure(ctx, row_id, stage, exc)}
    note = f"external spec cross-check failed at {stage} ({type(exc).__name__}: {exc})"
    ctx.emit(row_id, "cross_check_failed", stage=stage, error=str(exc))
    return {"hits": {}, "pages": {}, "extraction": None, "notes": [*state["notes"], note]}


async def estimate(state: ShouldCostState, runtime: Runtime[ShouldCostContext]) -> ShouldCostState:
    ctx, line = runtime.context, state["line"]
    part, extraction = state.get("part"), state.get("extraction")
    tolerance = ctx.settings.pipeline.spec_weight_tolerance
    external = verify_spec_pages(
        extraction.pages if extraction else [],
        state.get("pages", {}),
        current_oe=state.get("current_oe") or "",
        known_materials=state["known_materials"],
        tolerance=tolerance,
    )
    spec = reconcile_spec(
        internal_material=part.material if part else None,
        internal_weight_kg=part.weight_kg if part else None,
        external=external,
        tolerance=tolerance,
    )
    category, category_notes = _category(part, external, line.description)
    cycle, default_cycle = _cycle_time(part, category, ctx)
    try:
        breakdown, cost_notes = await _breakdown(spec, category, cycle, ctx)
    except Exception as exc:  # noqa: BLE001 - only the material price lookup can raise
        return {"failure": record_failure(ctx, line.row_id, "material_price", exc)}
    notes = [*state["notes"], *spec_notes(part, external, spec), *category_notes, *cost_notes]
    if default_cycle and breakdown is not None:
        notes.append(f"category default cycle time {cycle:g} min used")
    elif default_cycle:
        cycle = None  # nothing was costed, so no default cycle time was used
    if extraction is not None and extraction.reasoning:
        notes.append(f"llm: {extraction.reasoning}")
    result = ShouldCostResult(
        row_id=line.row_id,
        category=category,
        material=spec.material,
        weight_kg=spec.weight_kg,
        cycle_time_min=cycle,
        spec_source=spec.spec_source,
        breakdown=breakdown,
        status="estimated" if breakdown is not None else "insufficient_data",
        reasoning="; ".join(notes),
        evidence=_evidence(part, external),
    )
    ctx.emit(
        line.row_id,
        "estimate",
        llm_output=extraction,
        rejected=[{"page_id": r.page_id, "reason": r.reason} for r in external.rejected],
        material_source=spec.material_source,
        weight_source=spec.weight_source,
        result=result,
    )
    return {"result": result}


# --- estimate helpers -----------------------------------------------------------------


def spec_notes(part: PartSpec | None, external: ExternalSpec, spec: ReconciledSpec) -> list[str]:
    fields: tuple[tuple[str, SpecSource, str | float | None, ExternalField[Any]], ...] = (
        ("material", spec.material_source, part.material if part else None, external.material),
        ("weight", spec.weight_source, part.weight_kg if part else None, external.weight_kg),
    )
    notes = []
    for name, source, internal, ext in fields:
        if ext.disagree:
            notes.append(f"external pages disagree on {name}; needs manual review")
        elif source == "conflict":
            notes.append(
                f"{name} conflict: internal {_show(internal)} vs external {_show(ext.value)}; "
                "internal used"
            )
        if source == "external":
            notes.append(f"{name} missing internally; taken from the external spec page")
        elif source == "missing" and not ext.disagree:
            notes.append(f"{name} not available internally or externally")
    notes.append(f"spec source: {spec.spec_source}")
    return notes


def _show(value: str | float | None) -> str:
    return f"{value:.3f} kg" if isinstance(value, float) else str(value)


def _category(
    part: PartSpec | None, external: ExternalSpec, description: str | None
) -> tuple[str | None, list[str]]:
    """(category, reasoning notes): internal record, else OEM catalog page, else RFQ text."""
    if part is not None and part.category is not None:
        return part.category, []
    notes = ["category missing internally"] if part is not None else []
    if external.category.value is not None:
        category = external.category.value
        return category, [*notes, f"category {category} from the OEM catalog page"]
    if external.category.disagree:
        return None, [*notes, "OEM catalog pages disagree on the category; category unknown"]
    from_description = category_from_text(description) if description else None
    if from_description is not None:
        return from_description, [*notes, f"category {from_description} from the RFQ description"]
    return None, [*notes, "category unknown (no catalog page or RFQ description names it)"]


def _cycle_time(
    part: PartSpec | None, category: str | None, ctx: ShouldCostContext
) -> tuple[float | None, bool]:
    """(cycle time, whether it is the category default)."""
    if part is not None and part.cycle_time_min is not None:
        return part.cycle_time_min, False
    categories = ctx.settings.cost_model.categories
    if category is None or category not in categories:
        return None, False
    return categories[category].cycle_time_min, True


async def _breakdown(
    spec: ReconciledSpec, category: str | None, cycle: float | None, ctx: ShouldCostContext
) -> tuple[CostBreakdown | None, list[str]]:
    """(cost stack, reasoning notes); the stack is None when an input is missing."""
    if spec.spec_source == "missing" or spec.material is None or spec.weight_kg is None:
        return None, []
    if category is None or category not in ctx.settings.cost_model.categories:
        return None, [f"no cost-model rates for category {category}"]
    if spec.material == OTHER_MATERIAL:
        return None, ["material is not one of the known materials; no material price"]
    price = await ctx.internal_db.get_material_price(spec.material)
    if not price.found or price.price_per_kg is None:
        return None, [f"no internal material price for {spec.material}"]
    if price.currency is None:  # internal prices must state their currency
        return None, [f"material price for {spec.material} has no currency; not USD"]
    if price.currency.upper() != "USD":
        return None, [f"material price for {spec.material} is in {price.currency}, not USD"]
    breakdown = compute_breakdown(
        weight_kg=spec.weight_kg,
        material_price_per_kg=price.price_per_kg,
        category=category,
        cost_model=ctx.settings.cost_model,
        cycle_time_min=cycle,
    )
    return breakdown, []


def _evidence(part: PartSpec | None, external: ExternalSpec) -> list[Evidence]:
    evidence = []
    if part is not None:
        snippet = (
            f"{part.category}; material={part.material}; weight_kg={part.weight_kg}; "
            f"cycle_time_min={part.cycle_time_min}"
        )
        evidence.append(Evidence(source=INTERNAL_DB_SOURCE, ref=part.our_part_no, snippet=snippet))
    for field in (external.material, external.weight_kg, external.category):
        evidence.extend(field.evidence)
    return evidence


# --- graph ----------------------------------------------------------------------------


def build_graph() -> CompiledStateGraph[
    ShouldCostState, ShouldCostContext, ShouldCostState, ShouldCostState
]:
    graph = StateGraph(ShouldCostState, context_schema=ShouldCostContext)
    graph.add_node("prepare", prepare)
    graph.add_node("internal_spec", internal_spec)
    graph.add_node("search_external", search_external)
    graph.add_node("extract_spec", extract_spec)
    graph.add_node("estimate", estimate)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", ok_or_end("internal_spec"))
    graph.add_conditional_edges("internal_spec", ok_or_end("search_external"))
    graph.add_conditional_edges("search_external", ok_or_end("extract_spec"))
    graph.add_conditional_edges("extract_spec", ok_or_end("estimate"))
    graph.add_edge("estimate", END)
    # Never checkpointed: inside the orchestrator a row reruns whole if interrupted.
    return graph.compile(checkpointer=False)


GRAPH = build_graph()


async def resolve_should_cost(
    line: RFQLine, upstream: OEResolution | RowFailure, ctx: ShouldCostContext
) -> ShouldCostResult | RowFailure:
    """Run the subgraph for one row; any unexpected error becomes a row failure."""
    return await run_row(
        GRAPH,
        {"line": line, "upstream": upstream},
        ctx=ctx,
        row_id=line.row_id,
        result_type=ShouldCostResult,
        recursion_limit=20,
    )
