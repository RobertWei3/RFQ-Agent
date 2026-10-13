"""``retail_price`` agent (spec section 11.3): OE number -> retail quotes from five sites.

A per-row LangGraph subgraph:

    prepare -> search_sites -> extract_listings -> select

``prepare`` short-circuits rows without a unique OE number. Every site is searched
for every number on the supersession chain at once; one LLM call extracts the
facts of each listing page; deterministic code (``retail_rules``) matches part
numbers, keeps one quote per site, flags outliers and selects the reference quote.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TypedDict, cast

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel

from rfq_agent.agents.common import (
    AgentContext,
    RowFailure,
    ok_or_end,
    prepare_from_upstream,
    record_failure,
    render_pages,
    run_row,
    search_with_retry,
    split_chain_pages,
    upstream_row_id,
)
from rfq_agent.agents.retail_rules import (
    OutlierCheck,
    RetailListing,
    RetailPage,
    build_quotes,
    detect_outliers,
    select_retail,
)
from rfq_agent.schemas import OEResolution, PriceQuote, RetailResult
from rfq_agent.tools.base import ToolHit
from rfq_agent.tools.registry import RETAIL_SITE_TOOLS

AGENT = "retail_price"
SITES = tuple(RETAIL_SITE_TOOLS)


# --- LLM extraction schema (internal) -------------------------------------------------


class RetailListingExtraction(BaseModel):
    """Listing facts for every page in the search results."""

    listings: list[RetailListing]
    reasoning: str


RETAIL_SYSTEM = """You read retail web shop listings for an aftermarket auto-parts RFQ. For \
every page report: the part number shown in the listing title (listed_part_no), exactly as \
written; the regular / list / "was" price (list_price) and the discounted / sale / "now" \
price (sale_price), as plain numbers without currency symbols or thousands separators; the \
currency as an ISO code (null if the page does not say); whether the item is in stock \
(true / false, or null if the page does not say; "Ships today" counts as in stock); and the \
line stating the price \
(price_snippet). Use null for a price the page does not show. Never invent numbers or \
prices. Search results may include pages about unrelated parts; still report them."""


# --- context, state -------------------------------------------------------------------


@dataclass(kw_only=True)
class RetailContext(AgentContext):
    agent: str = AGENT


RetailInput = OEResolution | RowFailure


class RetailState(TypedDict, total=False):
    upstream: RetailInput
    chain: list[str]  # display numbers, oldest -> current
    hits: dict[str, ToolHit]
    page_sites: dict[str, str]  # page_id -> site
    failed_sites: dict[str, str]  # site -> error
    extraction: RetailListingExtraction | None
    failure: RowFailure | None
    result: RetailResult | None


# --- nodes ----------------------------------------------------------------------------


def empty_result(row_id: int, reasoning: str) -> RetailResult:
    """Result for a row that was not priced (no unique OE number)."""
    return RetailResult(
        row_id=row_id, oe_part_no=None, quotes=[], selected=None, outliers=[], reasoning=reasoning
    )


async def prepare(state: RetailState, runtime: Runtime[RetailContext]) -> RetailState:
    return cast(
        RetailState, prepare_from_upstream(runtime.context, state["upstream"], empty_result)
    )


@dataclass
class SiteSearch:
    """One site's hits for every chain number; ``error`` is set if any search failed."""

    site: str
    hits: list[ToolHit]
    error: str | None = None


async def search_sites(state: RetailState, runtime: Runtime[RetailContext]) -> RetailState:
    ctx, row_id, chain = runtime.context, upstream_row_id(state), state["chain"]
    searches = await asyncio.gather(*(_search_site(ctx, site, chain) for site in SITES))
    failed = {s.site: s.error for s in searches if s.error is not None}
    hits: dict[str, ToolHit] = {}
    page_sites: dict[str, str] = {}
    filtered: list[str] = []
    for search in searches:
        kept, decoys = split_chain_pages(search.hits, chain)
        filtered.extend(decoys)
        for hit in kept:
            if hit.page_id not in hits:
                hits[hit.page_id], page_sites[hit.page_id] = hit, search.site
    ctx.emit(
        row_id,
        "search_sites",
        failed_sites=failed,
        pages_filtered_out=sorted(set(filtered)),
        pages_kept=page_sites,
    )
    if len(failed) == len(SITES):
        error = RuntimeError("all retail site searches failed: " + "; ".join(failed.values()))
        return {"failure": record_failure(ctx, row_id, "search_sites", error)}
    return {"hits": hits, "page_sites": page_sites, "failed_sites": failed}


async def _search_site(ctx: RetailContext, site: str, chain: list[str]) -> SiteSearch:
    tool = ctx.tools[RETAIL_SITE_TOOLS[site]]
    try:
        results = await asyncio.gather(*(search_with_retry(tool, n) for n in chain))
    except Exception as exc:  # noqa: BLE001 - one failed site must not sink the row
        return SiteSearch(site, [], f"{type(exc).__name__}: {exc}")
    return SiteSearch(site, [hit for result in results for hit in result])


async def extract_listings(state: RetailState, runtime: Runtime[RetailContext]) -> RetailState:
    ctx, row_id, hits = runtime.context, upstream_row_id(state), state["hits"]
    if not hits:
        return {"extraction": None}
    messages: list[BaseMessage] = [
        SystemMessage(content=RETAIL_SYSTEM),
        HumanMessage(
            content="Supersession chain (oldest -> current): "
            + " -> ".join(state["chain"])
            + f"\n\n{render_pages(list(hits.values()))}"
        ),
    ]
    try:
        extraction = await ctx.extract(RetailListingExtraction, messages, row_id)
    except Exception as exc:  # noqa: BLE001
        return {"failure": record_failure(ctx, row_id, "extract_listings", exc)}
    return {"extraction": extraction}


async def select(state: RetailState, runtime: Runtime[RetailContext]) -> RetailState:
    ctx, row_id, chain = runtime.context, upstream_row_id(state), state["chain"]
    upstream = state["upstream"]
    assert isinstance(upstream, OEResolution)
    failed = state.get("failed_sites", {})
    extraction = state.get("extraction")
    pages = {
        pid: RetailPage(state["page_sites"][pid], hit.text) for pid, hit in state["hits"].items()
    }
    built = build_quotes(chain, extraction.listings if extraction else [], pages)
    check = detect_outliers(built.quotes, ctx.settings.pipeline.retail_outlier_threshold)
    selected = select_retail(built.quotes, check.outliers)
    reasoning = row_reasoning(
        built.quotes, check, selected, failed, extraction.reasoning if extraction else ""
    )
    result = RetailResult(
        row_id=row_id,
        oe_part_no=upstream.current_oe,
        quotes=built.quotes,
        selected=selected,
        outliers=check.outliers,
        reasoning=reasoning,
    )
    ctx.emit(
        row_id,
        "select",
        llm_output=extraction,
        rejected=[{"page_id": r.page_id, "reason": r.reason} for r in built.rejected],
        quotes={q.source: q.effective_price for q in built.quotes},
        outliers=check.outliers,
        median=check.median,
        selected=selected.source if selected else None,
        reasoning=reasoning,
    )
    return {"result": result}


def row_reasoning(
    quotes: list[PriceQuote],
    check: OutlierCheck,
    selected: PriceQuote | None,
    failed_sites: dict[str, str],
    llm_reasoning: str,
) -> str:
    """Row-level conclusion: failed sites, outliers, selection, then the LLM's note."""
    notes = [f"{site} search failed ({error})" for site, error in sorted(failed_sites.items())]
    notes += _outlier_notes(quotes, check)
    notes += _selection_notes(quotes, selected)
    if llm_reasoning:
        notes.append(f"llm: {llm_reasoning}")
    return "; ".join(notes)


def _outlier_notes(quotes: list[PriceQuote], check: OutlierCheck) -> list[str]:
    if not quotes:
        return ["no usable retail listing on any site"]
    if check.median is None:
        return [f"only {len(quotes)} quote(s); outlier detection needs at least 3"]
    notes = []
    for q in quotes:
        if q.source in check.outliers and q.effective_price is not None:
            deviation = (q.effective_price - check.median) / check.median
            notes.append(
                f"{q.source} {q.effective_price:.2f} is {deviation:+.0%} vs median "
                f"{check.median:.2f} (outlier)"
            )
    return notes


def _selection_notes(quotes: list[PriceQuote], selected: PriceQuote | None) -> list[str]:
    if selected is not None:
        stock = "in stock" if selected.in_stock else "stock not confirmed"
        return [
            f"selected {selected.source} ({selected.match_quality}, {stock}, "
            f"effective {selected.effective_price:.2f})"
        ]
    if quotes:
        return [
            "all quotes deviate from the median beyond the outlier threshold; "
            "no reliable reference price; needs manual review"
        ]
    return []


# --- graph ----------------------------------------------------------------------------


def _after_prepare(state: RetailState) -> str:
    return END if state.get("failure") or state.get("result") else "search_sites"


def build_graph() -> CompiledStateGraph[RetailState, RetailContext, RetailState, RetailState]:
    graph = StateGraph(RetailState, context_schema=RetailContext)
    graph.add_node("prepare", prepare)
    graph.add_node("search_sites", search_sites)
    graph.add_node("extract_listings", extract_listings)
    graph.add_node("select", select)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _after_prepare)
    graph.add_conditional_edges("search_sites", ok_or_end("extract_listings"))
    graph.add_conditional_edges("extract_listings", ok_or_end("select"))
    graph.add_edge("select", END)
    # Never checkpointed: inside the orchestrator a row reruns whole if interrupted.
    return graph.compile(checkpointer=False)


GRAPH = build_graph()


async def resolve_retail(upstream: RetailInput, ctx: RetailContext) -> RetailResult | RowFailure:
    """Run the subgraph for one row; any unexpected error becomes a row failure."""
    return await run_row(
        GRAPH,
        {"upstream": upstream},
        ctx=ctx,
        row_id=upstream.row_id,
        result_type=RetailResult,
        recursion_limit=20,
    )
