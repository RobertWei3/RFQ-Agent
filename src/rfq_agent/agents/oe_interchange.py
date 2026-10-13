"""``oe_interchange`` agent (spec section 11.1): customer part number -> OE number.

A per-row LangGraph subgraph. Deterministic code drives the tools; the LLM only
extracts candidates and supersession relations from page text. Chain building,
filtering and the final status decision are deterministic.

    lookup_our_part -> search -> extract_candidates -> expand_chain (loop) -> build_resolution
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from rfq_agent.agents.common import (
    AgentContext,
    RowFailure,
    ok_or_end,
    page_mentions,
    record_failure,
    render_pages,
    run_row,
    search_with_retry,
)
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import Evidence, OECandidate, OEResolution, RFQLine
from rfq_agent.tools.base import ToolHit

AGENT = "oe_interchange"
MAX_HOPS = 4

MatchType = Literal["exact", "normalized", "interchange", "llm_inferred"]
Stage = Literal["lookup_our_part", "search", "extract_candidates", "expand_chain", "build"]


# --- LLM extraction schemas (internal; not stage contracts) -------------------


class ExtractedCandidate(BaseModel):
    """One OE candidate listed on an interchange page."""

    oe_part_no: str = Field(description="OE part number exactly as written on the page.")
    brand: str | None = Field(default=None, description="OE brand / manufacturer, if stated.")
    page_id: str = Field(description="page_id of the page the candidate was taken from.")
    snippet: str | None = Field(default=None, description="The line that lists the candidate.")


class InterchangeExtraction(BaseModel):
    """OE candidates for one customer part number."""

    candidates: list[ExtractedCandidate]
    reasoning: str = Field(description="One or two sentences explaining the choice.")


class PageRelations(BaseModel):
    """Supersession facts stated on one OEM catalog page."""

    page_id: str
    subject_part_no: str | None = Field(
        description="The part number this catalog page is about (its own part number)."
    )
    brand: str | None = None
    superseded_by: str | None = Field(
        default=None, description="Newer number that replaces the subject, if stated."
    )
    replaces: str | None = Field(
        default=None, description="Older number that the subject replaces, if stated."
    )
    snippet: str | None = Field(default=None, description="Line stating the relation.")


class CatalogExtraction(BaseModel):
    """Supersession relations found on a set of OEM catalog pages."""

    pages: list[PageRelations]
    reasoning: str


INTERCHANGE_SYSTEM = """You extract OE (original equipment) part numbers for an aftermarket \
auto-parts RFQ. You receive search results for one customer part number. Only use the page \
whose lookup subject is that customer part number; ignore pages about other customer numbers. \
List every OE number the page presents as an interchange candidate, with its brand. Do NOT \
include numbers explicitly marked as similar but not interchangeable. If no page lists \
candidates for this customer number, return an empty list. Copy part numbers exactly as \
written; never invent numbers."""

CATALOG_SYSTEM = """You read OEM parts catalog pages and extract supersession facts. For each \
page give: the page's own part number (subject_part_no), the newer number it is superseded \
by / replaced by (superseded_by), and the older number it replaces (replaces). Use null when \
the page does not state a relation. Copy part numbers exactly as written; never invent \
numbers. Search results may include pages about unrelated parts; still report their subject."""


# --- context, state, outcomes -----------------------------------------------------


@dataclass(kw_only=True)
class OEContext(AgentContext):
    """Runtime dependencies for the oe_interchange subgraph."""

    lookup_our_part: Callable[[str], Awaitable[str | None]]
    agent: str = AGENT


@dataclass
class _Candidate:
    key: str
    display: str
    brand: str | None
    match_type: MatchType
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class _Edge:
    old: str
    new: str
    evidence: Evidence


class OEState(TypedDict, total=False):
    line: RFQLine
    our_part_no: str | None
    our_part_source: Literal["given", "xref", "none"]
    interchange_hits: list[ToolHit]
    candidates: list[_Candidate]
    edges: list[_Edge]
    display: dict[str, str]
    page_evidence: dict[str, list[Evidence]]
    frontier: list[str]
    visited: list[str]
    hops: int
    notes: list[str]
    failure: RowFailure | None
    result: OEResolution | None


class ChainError(ValueError):
    """Supersession relations contain a cycle or conflicting branches."""


# --- deterministic helpers ----------------------------------------------------------


def build_chains(edges: Sequence[tuple[str, str]], starts: Sequence[str]) -> dict[str, list[str]]:
    """Return ``{start: [head, ..., tail]}`` following old -> new edges both ways.

    Raises ``ChainError`` on cycles or when a number has two different
    successors or predecessors.
    """
    succ: dict[str, str] = {}
    pred: dict[str, str] = {}
    for old, new in edges:
        if old == new:
            continue
        if succ.get(old, new) != new:
            raise ChainError(f"{old} is superseded by both {succ[old]} and {new}")
        if pred.get(new, old) != old:
            raise ChainError(f"{new} replaces both {pred[new]} and {old}")
        succ[old], pred[new] = new, old

    chains: dict[str, list[str]] = {}
    for start in starts:
        head, seen = start, {start}
        while head in pred:
            head = pred[head]
            if head in seen:
                raise ChainError(f"supersession cycle through {head}")
            seen.add(head)
        chain, node = [head], head
        while node in succ:
            node = succ[node]
            if node in chain:
                raise ChainError(f"supersession cycle through {node}")
            chain.append(node)
        chains[start] = chain
    return chains


def _evidence(source: str, hit_id: str, snippet: str | None) -> Evidence:
    return Evidence(source=source, ref=hit_id, snippet=snippet)


def _fail(state: OEState, ctx: OEContext, stage: Stage, exc: BaseException) -> OEState:
    return {"failure": record_failure(ctx, state["line"].row_id, stage, exc)}


# --- nodes --------------------------------------------------------------------------


async def lookup_our_part(state: OEState, runtime: Runtime[OEContext]) -> OEState:
    line, ctx = state["line"], runtime.context
    ctx.emit(line.row_id, "start", line=line)
    if line.our_part_no:
        ctx.emit(line.row_id, "lookup_our_part", source="given", our_part_no=line.our_part_no)
        return {"our_part_no": line.our_part_no, "our_part_source": "given"}
    try:
        found = await ctx.lookup_our_part(line.customer_part_no)
    except Exception as exc:  # noqa: BLE001 - recorded as a row failure
        return _fail(state, ctx, "lookup_our_part", exc)
    source: Literal["xref", "none"] = "xref" if found else "none"
    ctx.emit(line.row_id, "lookup_our_part", source=f"mcp:{source}", our_part_no=found)
    return {"our_part_no": found, "our_part_source": source}


async def search(state: OEState, runtime: Runtime[OEContext]) -> OEState:
    ctx, line = runtime.context, state["line"]
    try:
        hits = await search_with_retry(ctx.tools["interchange_search"], line.customer_part_no)
    except Exception as exc:  # noqa: BLE001
        return _fail(state, ctx, "search", exc)
    ctx.emit(
        line.row_id,
        "search",
        tool="interchange_search",
        query=line.customer_part_no,
        hits=[{"page_id": h.page_id, "title": h.title} for h in hits],
    )
    return {"interchange_hits": hits}


async def extract_candidates(state: OEState, runtime: Runtime[OEContext]) -> OEState:
    line, ctx = state["line"], runtime.context
    key = line.customer_part_no
    # Deterministic pre-filter: decoy pages about other customer numbers never reach the LLM.
    all_hits = state.get("interchange_hits", [])
    hits = [h for h in all_hits if page_mentions(h.text, key)]
    extracted: InterchangeExtraction | None = None
    candidates: list[_Candidate] = []
    notes: list[str] = []
    if hits:
        messages: list[BaseMessage] = [
            SystemMessage(content=INTERCHANGE_SYSTEM),
            HumanMessage(
                content=f"Customer part number: {line.customer_part_no_raw.strip()}\n\n"
                f"{render_pages(hits)}"
            ),
        ]
        try:
            extraction = await ctx.extract(InterchangeExtraction, messages, line.row_id)
        except Exception as exc:  # noqa: BLE001
            return _fail(state, ctx, "extract_candidates", exc)
        assert isinstance(extraction, InterchangeExtraction)
        extracted = extraction
        texts = {h.page_id: h.text for h in hits}
        for item in extraction.candidates:
            cand_key = normalize_part_no(item.oe_part_no)
            if not cand_key or any(c.key == cand_key for c in candidates):
                continue
            grounded = page_mentions(texts.get(item.page_id, ""), cand_key)
            candidates.append(
                _Candidate(
                    key=cand_key,
                    display=item.oe_part_no.strip(),
                    brand=item.brand,
                    match_type="interchange" if grounded else "llm_inferred",
                    evidence=[_evidence("interchange", item.page_id, item.snippet)],
                )
            )
        notes.append(f"interchange: {extraction.reasoning}")
    else:
        notes.append("interchange: no page for this customer part number")

    grounded_keys = [c.key for c in candidates if c.match_type != "llm_inferred"]
    # The customer number itself is also looked up in the catalog (it may already be an OE number).
    frontier = list(dict.fromkeys([key, *grounded_keys]))
    ctx.emit(
        line.row_id,
        "extract_candidates",
        pages_sent_to_llm=[h.page_id for h in hits],
        pages_filtered_out=[h.page_id for h in all_hits if h not in hits],
        llm_output=extracted,
        candidates=[{"oe_part_no": c.display, "match_type": c.match_type} for c in candidates],
        next_lookup=frontier,
    )
    return {
        "candidates": candidates,
        "edges": [],
        "display": {},
        "page_evidence": {},
        "frontier": frontier,
        "visited": [],
        "hops": 0,
        "notes": notes,
    }


async def expand_chain(state: OEState, runtime: Runtime[OEContext]) -> OEState:
    ctx = runtime.context
    frontier = state["frontier"]
    tool = ctx.tools["oem_catalog_search"]
    try:
        results = await asyncio.gather(*(search_with_retry(tool, k) for k in frontier))
    except Exception as exc:  # noqa: BLE001
        return _fail(state, ctx, "expand_chain", exc)

    row_id, hop = state["line"].row_id, state["hops"] + 1
    queried = set(frontier)
    hits: dict[str, ToolHit] = {}
    filtered: list[str] = []
    for result in results:
        for hit in result:
            if any(page_mentions(hit.text, k) for k in queried):
                hits.setdefault(hit.page_id, hit)
            elif hit.page_id not in filtered:
                filtered.append(hit.page_id)
    searched = {k: [h.page_id for h in r] for k, r in zip(frontier, results, strict=True)}

    visited = [*state["visited"], *frontier]
    update: OEState = {"visited": visited, "hops": hop, "frontier": []}
    if not hits:
        ctx.emit(
            row_id,
            "expand_chain",
            hop=hop,
            searched=searched,
            pages_filtered_out=filtered,
            pages_sent_to_llm=[],
            next_lookup=[],
        )
        return update

    messages: list[BaseMessage] = [
        SystemMessage(content=CATALOG_SYSTEM),
        HumanMessage(
            content="Part numbers being looked up: "
            + ", ".join(sorted(queried))
            + f"\n\n{render_pages(list(hits.values()))}"
        ),
    ]
    try:
        extraction = await ctx.extract(CatalogExtraction, messages, row_id)
    except Exception as exc:  # noqa: BLE001
        return _fail(state, ctx, "expand_chain", exc)
    assert isinstance(extraction, CatalogExtraction)

    edges = list(state["edges"])
    display = dict(state["display"])
    page_evidence = {k: list(v) for k, v in state["page_evidence"].items()}
    candidates = list(state["candidates"])
    line = state["line"]
    new_numbers: list[str] = []
    dropped: list[dict[str, str | None]] = []
    edges_before = len(edges)

    for page in extraction.pages:
        subject = normalize_part_no(page.subject_part_no or "")
        page_hit = hits.get(page.page_id)
        # Keep only pages about a number we asked for (drops decoys) and grounded in the text.
        if page_hit is None or subject not in queried or not page_mentions(page_hit.text, subject):
            if page_hit is None:
                reason = "unknown page_id"
            elif subject not in queried:
                reason = "subject is not a looked-up number"
            else:
                reason = "subject not found in page text"
            dropped.append(
                {"page_id": page.page_id, "subject": page.subject_part_no, "reason": reason}
            )
            continue
        display.setdefault(subject, (page.subject_part_no or subject).strip())
        evidence = _evidence("oem_catalog", page.page_id, page.snippet)
        page_evidence.setdefault(subject, []).append(evidence)

        if subject == line.customer_part_no and not any(c.key == subject for c in candidates):
            written = (page.subject_part_no or "").strip()
            exact = written == line.customer_part_no_raw.strip()
            candidates.append(
                _Candidate(
                    subject, written, page.brand, "exact" if exact else "normalized", [evidence]
                )
            )

        for other, direction in ((page.superseded_by, "new"), (page.replaces, "old")):
            other_key = normalize_part_no(other or "")
            if not other_key:
                continue
            if not page_mentions(page_hit.text, other_key):
                dropped.append(
                    {
                        "page_id": page.page_id,
                        "subject": other,
                        "reason": "relation not in page text",
                    }
                )
                continue
            display.setdefault(other_key, (other or other_key).strip())
            old, new = (subject, other_key) if direction == "new" else (other_key, subject)
            edges.append(_Edge(old, new, evidence))
            if other_key not in visited and other_key not in new_numbers:
                new_numbers.append(other_key)

    ctx.emit(
        row_id,
        "expand_chain",
        hop=hop,
        searched=searched,
        pages_filtered_out=filtered,
        pages_sent_to_llm=list(hits),
        llm_output=extraction,
        dropped=dropped,
        edges_added=[f"{e.old} -> {e.new}" for e in edges[edges_before:]],
        next_lookup=new_numbers,
    )
    update.update(
        {
            "edges": edges,
            "display": display,
            "page_evidence": page_evidence,
            "candidates": candidates,
            "frontier": new_numbers,
            "notes": [*state["notes"], f"catalog hop {state['hops'] + 1}: {extraction.reasoning}"],
        }
    )
    return update


async def build_resolution(state: OEState, runtime: Runtime[OEContext]) -> OEState:
    ctx = runtime.context
    try:
        result = resolve(state)
    except ChainError as exc:
        return _fail(state, ctx, "build", exc)
    ctx.emit(
        result.row_id,
        "build_resolution",
        edges=[f"{e.old} -> {e.new}" for e in state.get("edges", [])],
        status=result.status,
        current_oe=result.current_oe,
        supersession_chain=result.supersession_chain,
        candidates=[
            {"oe_part_no": c.oe_part_no, "match_type": c.match_type} for c in result.candidates
        ],
    )
    return {"result": result}


def resolve(state: OEState) -> OEResolution:
    """Deterministic chain building and status decision (plan decision Q5)."""
    line = state["line"]
    display = state.get("display", {})
    page_evidence = state.get("page_evidence", {})
    candidates = state.get("candidates", [])
    grounded = [c for c in candidates if c.match_type != "llm_inferred"]
    chains = build_chains(
        [(e.old, e.new) for e in state.get("edges", [])], [c.key for c in grounded]
    )

    by_tail: dict[str, OECandidate] = {}
    tail_chain: dict[str, list[str]] = {}
    for cand in grounded:
        chain = chains[cand.key]
        tail = chain[-1]
        evidence = [*cand.evidence, *(ev for k in chain for ev in page_evidence.get(k, []))]
        existing = by_tail.get(tail)
        if existing is None:
            by_tail[tail] = OECandidate(
                oe_part_no=display.get(tail, cand.display if tail == cand.key else tail),
                brand=cand.brand,
                match_type=cand.match_type,
                evidence=_dedupe(evidence),
            )
            tail_chain[tail] = chain
        else:
            existing.evidence = _dedupe([*existing.evidence, *evidence])

    out_candidates = list(by_tail.values())
    for cand in candidates:
        if cand.match_type == "llm_inferred":
            out_candidates.append(
                OECandidate(
                    oe_part_no=cand.display,
                    brand=cand.brand,
                    match_type="llm_inferred",
                    evidence=cand.evidence,
                )
            )

    if not by_tail:
        status: Literal["resolved", "multiple", "not_found"] = "not_found"
        current, chain_display = None, []
        summary = "No grounded OE candidate was found."
    elif len(by_tail) == 1:
        status = "resolved"
        (tail,) = by_tail
        current = by_tail[tail].oe_part_no
        chain_display = [display.get(k, k) for k in tail_chain[tail]]
        summary = f"Resolved to {current}" + (
            f" via supersession chain {' -> '.join(chain_display)}."
            if len(chain_display) > 1
            else "."
        )
    else:
        status = "multiple"
        current, chain_display = None, []
        summary = "Several distinct current OE numbers: " + ", ".join(
            c.oe_part_no for c in by_tail.values()
        )

    # Numbers still waiting to be followed means the hop limit cut the chain short.
    truncated = bool(state.get("frontier"))
    if truncated:
        summary += f" Chain expansion stopped at the {MAX_HOPS}-hop limit."
    return OEResolution(
        row_id=line.row_id,
        our_part_no=state.get("our_part_no"),
        our_part_source=state.get("our_part_source", "none"),
        status=status,
        current_oe=current,
        supersession_chain=chain_display,
        chain_truncated=truncated,
        candidates=out_candidates,
        reasoning=" ".join([*state.get("notes", []), summary]),
    )


def _dedupe(evidence: list[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, str]] = set()
    out = []
    for ev in evidence:
        if (ev.source, ev.ref) not in seen:
            seen.add((ev.source, ev.ref))
            out.append(ev)
    return out


# --- graph --------------------------------------------------------------------------


def _after_expand(state: OEState) -> str:
    if state.get("failure"):
        return END
    if state.get("frontier") and state["hops"] < MAX_HOPS:
        return "expand_chain"
    return "build_resolution"


def build_graph() -> CompiledStateGraph[OEState, OEContext, OEState, OEState]:
    graph = StateGraph(OEState, context_schema=OEContext)
    graph.add_node("lookup_our_part", lookup_our_part)
    graph.add_node("search", search)
    graph.add_node("extract_candidates", extract_candidates)
    graph.add_node("expand_chain", expand_chain)
    graph.add_node("build_resolution", build_resolution)
    graph.add_edge(START, "lookup_our_part")
    graph.add_conditional_edges("lookup_our_part", ok_or_end("search"))
    graph.add_conditional_edges("search", ok_or_end("extract_candidates"))
    graph.add_conditional_edges("extract_candidates", ok_or_end("expand_chain"))
    graph.add_conditional_edges("expand_chain", _after_expand)
    graph.add_edge("build_resolution", END)
    # Never checkpointed: inside the orchestrator a row reruns whole if interrupted.
    return graph.compile(checkpointer=False)


GRAPH = build_graph()


async def resolve_line(line: RFQLine, ctx: OEContext) -> OEResolution | RowFailure:
    """Run the subgraph for one line; any unexpected error becomes a row failure."""
    return await run_row(
        GRAPH,
        {"line": line},
        ctx=ctx,
        row_id=line.row_id,
        result_type=OEResolution,
        recursion_limit=30,
    )
