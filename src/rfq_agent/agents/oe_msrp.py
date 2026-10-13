"""``oe_msrp`` agent (spec section 11.2): OE number -> manufacturer's suggested retail price.

A per-row LangGraph subgraph:

    prepare -> search_chain -> extract_prices -> select

``prepare`` short-circuits rows without a unique OE number. All numbers on the
supersession chain are searched at once; one LLM call extracts price facts per
page; deterministic code walks the chain from the current number back to the
first number with a grounded price.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from rfq_agent.agents.common import (
    AgentContext,
    RowFailure,
    non_usd,
    ok_or_end,
    page_mentions,
    prepare_from_upstream,
    price_in_text,
    record_failure,
    render_pages,
    run_row,
    search_with_retry,
    split_chain_pages,
    upstream_row_id,
)
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import Evidence, MSRPResult, OEResolution, PriceQuote
from rfq_agent.tools.base import ToolHit

AGENT = "oe_msrp"
SOURCE = "msrp_lookup"

Status = Literal["found", "not_found", "discontinued"]


# --- LLM extraction schema (internal) -------------------------------------------------


class MSRPPage(BaseModel):
    """Price facts stated on one manufacturer price page."""

    page_id: str
    subject_part_no: str | None = Field(
        description="The part number this page is about, exactly as written."
    )
    price: float | None = Field(
        default=None, description="MSRP / list price shown on the page, as a plain number."
    )
    currency: str | None = Field(default=None, description="ISO currency code, e.g. USD.")
    discontinued: bool = Field(default=False, description="Page says the part is discontinued.")
    snippet: str | None = Field(default=None, description="The line stating the price.")
    discontinued_snippet: str | None = Field(
        default=None, description="The line saying the part is discontinued, if any."
    )


class MSRPPageExtraction(BaseModel):
    """Price facts for every page in the search results."""

    pages: list[MSRPPage]
    reasoning: str


MSRP_SYSTEM = """You read manufacturer (OEM) parts price pages for an aftermarket auto-parts \
RFQ. For every page report: the part number the page is about (subject_part_no), the MSRP / \
suggested retail / list price it shows (price, a plain number without currency symbols or \
thousands separators), the currency as an ISO code (null if the page does not say), and \
whether the page says the part is discontinued (and copy that line as discontinued_snippet). \
Use null when the page shows no price. Copy \
part numbers exactly as written; never invent numbers or prices. Search results may include \
pages about unrelated parts; still report them."""


# --- context, state -----------------------------------------------------------------


@dataclass(kw_only=True)
class MSRPContext(AgentContext):
    agent: str = AGENT


MSRPInput = OEResolution | RowFailure


class MSRPState(TypedDict, total=False):
    upstream: MSRPInput
    chain: list[str]  # display numbers, oldest -> current
    hits: dict[str, ToolHit]
    extraction: MSRPPageExtraction | None
    failure: RowFailure | None
    result: MSRPResult | None


# --- deterministic selection ----------------------------------------------------------


class RejectedPage(BaseModel):
    """An extracted page fact that deterministic checks rejected (kept for the trace)."""

    page_id: str
    subject: str | None
    reason: str


@dataclass
class Selection:
    status: Status
    quote: PriceQuote | None
    reasoning: str
    dropped: list[RejectedPage]


@dataclass
class _Screened:
    """Extracted pages sorted by chain number after the grounding checks."""

    priced: dict[str, list[MSRPPage]]
    problem: dict[str, str]  # key -> why that number has no usable price
    dropped: list[RejectedPage]


def select_msrp(
    chain: Sequence[str],
    pages: Sequence[MSRPPage],
    texts: dict[str, str],
    llm_reasoning: str = "",
) -> Selection:
    """Pick the MSRP for the current (last) number of ``chain``, walking back as needed.

    A price is usable only if its page is about a chain number, the price appears
    in the page text and it is in USD (a page without a currency counts as USD).
    Conflicting prices for one number: the higher wins. Discontinued is read from
    the page that supplied the chosen price.
    """
    keys = [normalize_part_no(n) for n in chain]
    display = dict(zip(keys, chain, strict=True))
    screened = _screen(pages, texts, display)
    notes = [f"llm: {llm_reasoning}"] if llm_reasoning else []
    for key in reversed(keys):
        entries = screened.priced.get(key)
        if not entries:
            missing = (
                f"current OE {display[key]} has no price page"
                if key == keys[-1]
                else f"no price page for {display[key]}"
            )
            notes.append(screened.problem.get(key, missing))
            continue
        best = max(entries, key=lambda p: p.price or 0.0)
        if len({p.price for p in entries}) > 1:
            listed = ", ".join(f"{p.price} ({p.page_id})" for p in entries)
            notes.append(f"price conflict for {display[key]}: {listed}; took the higher")
        if key != keys[-1]:
            # Why the current number had no usable price was noted above; state the fallback once.
            notes.append(f"used MSRP of older number {display[key]} (supersession fallback)")
        if best.discontinued:
            notes.append(f"{display[key]} is marked discontinued on the price page")
        reasoning = "; ".join(notes)
        quote = _quote(best, entries, display[key], reasoning)
        status: Status = "discontinued" if best.discontinued else "found"
        return Selection(status, quote, reasoning, screened.dropped)
    return Selection("not_found", None, "; ".join(notes), screened.dropped)


def _screen(pages: Sequence[MSRPPage], texts: dict[str, str], display: dict[str, str]) -> _Screened:
    out = _Screened(priced={}, problem={}, dropped=[])
    for page in pages:
        subject = normalize_part_no(page.subject_part_no or "")
        text = texts.get(page.page_id)
        reason = None
        if text is None:
            reason = "unknown page_id"
        elif subject not in display:
            reason = "subject is not on the supersession chain"
        elif not page_mentions(text, subject):
            reason = "subject not found in page text"
        if reason is not None:
            out.dropped.append(
                RejectedPage(page_id=page.page_id, subject=page.subject_part_no, reason=reason)
            )
            continue
        assert text is not None
        name = display[subject]
        if page.price is None:
            out.problem.setdefault(subject, f"{name} page shows no price")
            continue
        rejected = _price_rejection(page.price, page.currency, text)
        if rejected is None:
            out.priced.setdefault(subject, []).append(page)
            continue
        note, reason = rejected
        out.problem[subject] = f"{name} {note}"
        out.dropped.append(
            RejectedPage(page_id=page.page_id, subject=page.subject_part_no, reason=reason)
        )
    return out


def _price_rejection(price: float, currency: str | None, text: str) -> tuple[str, str] | None:
    """(reasoning note, trace reason) when a stated price cannot be used, else None."""
    if price <= 0:
        return f"non-positive price ({price}) not adopted", f"non-positive price {price}"
    if not price_in_text(price, text):
        return "price could not be verified in the page text", (
            f"price {price} not found in page text"
        )
    code = non_usd(currency)
    if code is not None:
        return f"non-USD price ({code}) not adopted", f"non-USD price ({code}); only USD is used"
    return None


def _quote(best: MSRPPage, entries: list[MSRPPage], name: str, reasoning: str) -> PriceQuote:
    price = float(best.price or 0.0)
    written = (best.subject_part_no or "").strip()
    return PriceQuote(
        source=SOURCE,
        price_type="msrp",
        matched_part_no=written or name,
        list_price=price,
        sale_price=None,
        discount_pct=None,
        effective_price=price,
        currency="USD",
        in_stock=None,
        match_quality="exact" if written == name else "normalized",
        reasoning=reasoning,
        evidence=_evidence(best, entries),
    )


def _evidence(best: MSRPPage, entries: list[MSRPPage]) -> list[Evidence]:
    """All usable (grounded, USD) price pages, plus the chosen page's discontinued line."""
    evidence = [Evidence(source=SOURCE, ref=p.page_id, snippet=p.snippet) for p in entries]
    if best.discontinued and best.discontinued_snippet:
        evidence.append(
            Evidence(source=SOURCE, ref=best.page_id, snippet=best.discontinued_snippet)
        )
    return evidence


# --- nodes ----------------------------------------------------------------------------


def _not_found(row_id: int, oe: str | None, reasoning: str) -> MSRPResult:
    return MSRPResult(
        row_id=row_id, oe_part_no=oe, quote=None, status="not_found", reasoning=reasoning
    )


async def prepare(state: MSRPState, runtime: Runtime[MSRPContext]) -> MSRPState:
    def skipped(row_id: int, reason: str) -> MSRPResult:
        return _not_found(row_id, None, reason)

    return cast(MSRPState, prepare_from_upstream(runtime.context, state["upstream"], skipped))


async def search_chain(state: MSRPState, runtime: Runtime[MSRPContext]) -> MSRPState:
    ctx, row_id, chain = runtime.context, upstream_row_id(state), state["chain"]
    tool = ctx.tools[SOURCE]
    try:
        results = await asyncio.gather(*(search_with_retry(tool, n) for n in chain))
    except Exception as exc:  # noqa: BLE001 - recorded as a row failure
        return {"failure": record_failure(ctx, row_id, "search_chain", exc)}
    kept, filtered = split_chain_pages((hit for result in results for hit in result), chain)
    hits: dict[str, ToolHit] = {}
    for hit in kept:
        hits.setdefault(hit.page_id, hit)
    ctx.emit(
        row_id,
        "search_chain",
        searched={n: [h.page_id for h in r] for n, r in zip(chain, results, strict=True)},
        pages_filtered_out=filtered,
        pages_kept=list(hits),
    )
    return {"hits": hits}


async def extract_prices(state: MSRPState, runtime: Runtime[MSRPContext]) -> MSRPState:
    ctx, row_id, hits = runtime.context, upstream_row_id(state), state["hits"]
    if not hits:
        return {"extraction": None}
    messages: list[BaseMessage] = [
        SystemMessage(content=MSRP_SYSTEM),
        HumanMessage(
            content="Supersession chain (oldest -> current): "
            + " -> ".join(state["chain"])
            + f"\n\n{render_pages(list(hits.values()))}"
        ),
    ]
    try:
        extraction = await ctx.extract(MSRPPageExtraction, messages, row_id)
    except Exception as exc:  # noqa: BLE001
        return {"failure": record_failure(ctx, row_id, "extract_prices", exc)}
    return {"extraction": extraction}


async def select(state: MSRPState, runtime: Runtime[MSRPContext]) -> MSRPState:
    ctx, row_id, chain = runtime.context, upstream_row_id(state), state["chain"]
    upstream = state["upstream"]
    assert isinstance(upstream, OEResolution)
    extraction = state.get("extraction")
    if extraction is None:
        result = _not_found(
            row_id, upstream.current_oe, "No MSRP page found for any number on the chain."
        )
        ctx.emit(row_id, "select", status=result.status, reasoning=result.reasoning)
        return {"result": result}
    texts = {pid: hit.text for pid, hit in state["hits"].items()}
    selection = select_msrp(chain, extraction.pages, texts, extraction.reasoning)
    result = MSRPResult(
        row_id=row_id,
        oe_part_no=upstream.current_oe,
        quote=selection.quote,
        status=selection.status,
        reasoning=selection.reasoning,
    )
    ctx.emit(
        row_id,
        "select",
        llm_output=extraction,
        dropped=selection.dropped,
        status=result.status,
        msrp=selection.quote.effective_price if selection.quote else None,
        matched_part_no=selection.quote.matched_part_no if selection.quote else None,
        reasoning=result.reasoning,
    )
    return {"result": result}


# --- graph ----------------------------------------------------------------------------


def _after_prepare(state: MSRPState) -> str:
    return END if state.get("failure") or state.get("result") else "search_chain"


def build_graph() -> CompiledStateGraph[MSRPState, MSRPContext, MSRPState, MSRPState]:
    graph = StateGraph(MSRPState, context_schema=MSRPContext)
    graph.add_node("prepare", prepare)
    graph.add_node("search_chain", search_chain)
    graph.add_node("extract_prices", extract_prices)
    graph.add_node("select", select)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", _after_prepare)
    graph.add_conditional_edges("search_chain", ok_or_end("extract_prices"))
    graph.add_conditional_edges("extract_prices", ok_or_end("select"))
    graph.add_edge("select", END)
    # Never checkpointed: inside the orchestrator a row reruns whole if interrupted.
    return graph.compile(checkpointer=False)


GRAPH = build_graph()


async def resolve_msrp(upstream: MSRPInput, ctx: MSRPContext) -> MSRPResult | RowFailure:
    """Run the subgraph for one row; any unexpected error becomes a row failure."""
    return await run_row(
        GRAPH,
        {"upstream": upstream},
        ctx=ctx,
        row_id=upstream.row_id,
        result_type=MSRPResult,
        recursion_limit=20,
    )
