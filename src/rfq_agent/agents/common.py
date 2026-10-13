"""Building blocks shared by the subagents."""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from rfq_agent.config import Settings
from rfq_agent.llm.structured import invoke_structured
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import OEResolution, RowFailure
from rfq_agent.tools.base import ToolHit, ToolProvider, ToolTimeoutError
from rfq_agent.trace import META_ROW_ID, TraceLLMCallback, TraceWriter

# run_row pins durability on purpose (see there); the subgraph has no checkpointer.
warnings.filterwarnings(
    "ignore", message="`durability` has no effect when no checkpointer", category=UserWarning
)

PAGE_MARKER = "=== page_id:"

__all__ = [
    "PAGE_MARKER",
    "AgentContext",
    "RowFailure",
    "UpstreamCheck",
    "check_upstream",
    "non_usd",
    "prepare_from_upstream",
    "split_chain_pages",
    "upstream_row_id",
    "ok_or_end",
    "page_mentions",
    "price_in_text",
    "record_failure",
    "render_pages",
    "run_row",
    "search_with_retry",
]

MULTIPLE_OE_REASON = "OE not uniquely determined (multiple candidates); waiting for manual choice."
NO_OE_REASON = "No OE number was resolved for this row."

_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-/_]*[0-9A-Za-z]|[0-9A-Za-z]")
# A number that stands alone: not glued to letters, dashes or slashes (part numbers, units).
_STANDALONE_NUMBER = re.compile(r"(?<![\w.\-/])\d[\d,]*(?:\.\d+)?(?![\w\-/%]|\.\d)")
_CURRENCY_BEFORE = re.compile(r"(?:\$|\b[A-Z]{3})\s*$")
_CURRENCY_AFTER = re.compile(r"^\s*[A-Z]{3}\b")
_PRICE_LINE = re.compile(r"\b(?:price|msrp|list|sale|was|now|each|reg)\b", re.I)
_UNIT_AFTER = re.compile(r"^\s*[A-Za-z]")

S = TypeVar("S", bound=BaseModel)
R = TypeVar("R", bound=BaseModel)


@dataclass(kw_only=True)
class AgentContext:
    """Runtime dependencies shared by every agent subgraph."""

    agent: str
    tools: Mapping[str, ToolProvider]
    settings: Settings
    run_id: str | None = None
    model: BaseChatModel | None = None
    trace: TraceWriter | None = None
    _callbacks: list[BaseCallbackHandler] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.trace is not None:
            self._callbacks = [TraceLLMCallback(self.trace)]

    def emit(self, row_id: int, step: str, **data: object) -> None:
        """Record one agent step in the local trace (no-op when tracing is off)."""
        if self.trace is not None:
            self.trace.event(row_id, self.agent, step, **data)

    async def extract(self, schema: type[S], messages: list[BaseMessage], row_id: int) -> S:
        """Structured LLM extraction for one row; raises when retries are exhausted."""
        result = await invoke_structured(
            schema,
            messages,
            role=self.agent,
            run_id=self.run_id,
            agent=self.agent,
            model=self.model,
            settings=self.settings,
            metadata={META_ROW_ID: row_id},
            callbacks=self._callbacks,
        )
        self.emit(
            row_id,
            "structured_result",
            schema=schema.__name__,
            ok=result.ok,
            attempts=result.attempts,
            error=result.error,
        )
        if not result.ok or result.value is None:
            raise RuntimeError(result.error or "structured extraction failed")
        return result.value


MAX_SPLIT_TOKENS = 4  # "0B7 379 712J" style spellings split a number into several tokens


def page_mentions(text: str, key: str) -> bool:
    """True if ``key`` (normalized) appears in ``text`` as up to four adjacent tokens."""
    tokens = [normalize_part_no(t) for t in _TOKEN.findall(text)]
    for start in range(len(tokens)):
        joined = ""
        for token in tokens[start : start + MAX_SPLIT_TOKENS]:
            joined += token
            if joined == key:
                return True
            if not key.startswith(joined):
                break
    return False


def price_in_text(value: float, text: str) -> bool:
    """True if ``value`` appears in ``text`` as an amount of money (spec 11.2 / 11.3).

    Money means a standalone number with a currency mark ("$25.75", "25.75 USD",
    "USD 25.75") or a standalone number on a price line ("Price: 25.75") that is not
    followed by a unit word. Digits inside part numbers ("0B7-379-712J") or next to
    units ("12,000 miles") never count.
    """
    for line in text.splitlines():
        for match in _STANDALONE_NUMBER.finditer(line):
            if abs(float(match.group().replace(",", "")) - value) >= 0.005:
                continue
            before, after = line[: match.start()], line[match.end() :]
            if _CURRENCY_BEFORE.search(before) or _CURRENCY_AFTER.match(after):
                return True
            if _PRICE_LINE.search(line) and not _UNIT_AFTER.match(after):
                return True
    return False


def render_pages(hits: Sequence[ToolHit]) -> str:
    return "\n\n".join(f"{PAGE_MARKER} {h.page_id}\ntitle: {h.title}\n{h.text}" for h in hits)


async def search_with_retry(tool: ToolProvider, query: str) -> list[ToolHit]:
    """One retry on an (injected or real) timeout; a second timeout propagates."""
    try:
        return await tool.search(query)
    except ToolTimeoutError:
        return await tool.search(query)


# --- subgraph plumbing shared by every agent ------------------------------------------


def record_failure(ctx: AgentContext, row_id: int, stage: str, exc: BaseException) -> RowFailure:
    """Turn a node exception into a row failure and record it in the trace."""
    error = f"{type(exc).__name__}: {exc}"
    ctx.emit(row_id, "failure", stage=stage, error=error)
    return RowFailure(row_id=row_id, stage=stage, error=error)


def ok_or_end(next_node: str) -> Callable[[Mapping[str, Any]], str]:
    """Conditional edge: stop the subgraph once a node has recorded a failure."""

    def route(state: Mapping[str, Any]) -> str:
        return END if state.get("failure") else next_node

    return route


async def run_row(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    inputs: dict[str, Any],
    *,
    ctx: AgentContext,
    row_id: int,
    result_type: type[R],
    recursion_limit: int,
) -> R | RowFailure:
    """Run one row through ``graph``; any unexpected error becomes a row failure."""
    try:
        # Agent subgraphs have no checkpointer. Inside the orchestrator they would inherit the
        # parent's durability; "sync" then waits on a checkpoint that is never written
        # (langgraph 1.2.12), so pin the mode that writes nothing.
        state = await graph.ainvoke(
            inputs,
            context=ctx,
            config={"recursion_limit": recursion_limit},
            durability="exit",
        )
    except Exception as exc:  # noqa: BLE001 - one bad row must not crash the batch
        return record_failure(ctx, row_id, "graph", exc)
    failure = state.get("failure")
    if isinstance(failure, RowFailure):
        return failure
    result = state.get("result")
    if isinstance(result, result_type):
        return result
    return RowFailure(row_id=row_id, stage="graph", error="graph ended without a result")


# --- upstream OE short-circuit (price agents) -----------------------------------------


@dataclass
class UpstreamCheck:
    """What a price agent should do with one oe_interchange outcome.

    Exactly one of ``failure`` (upstream failed), ``skip_reason`` (no unique OE:
    output an empty result without calling tools or the LLM) or ``chain``
    (numbers to price, oldest -> current) is set.
    """

    failure: RowFailure | None = None
    skip_reason: str | None = None
    chain: list[str] = field(default_factory=list)


def check_upstream(upstream: OEResolution | RowFailure) -> UpstreamCheck:
    """Shared rule for oe_msrp and retail_price (spec sections 11.2 and 11.3)."""
    if isinstance(upstream, RowFailure):
        return UpstreamCheck(
            failure=RowFailure(
                row_id=upstream.row_id,
                stage="upstream_oe",
                error=f"oe_interchange failed at {upstream.stage}: {upstream.error}",
            )
        )
    if upstream.status == "multiple":
        return UpstreamCheck(skip_reason=MULTIPLE_OE_REASON)
    if upstream.status == "not_found" or not upstream.current_oe:
        return UpstreamCheck(skip_reason=NO_OE_REASON)
    chain = list(upstream.supersession_chain) or [upstream.current_oe]
    if normalize_part_no(chain[-1]) == normalize_part_no(upstream.current_oe):
        chain[-1] = upstream.current_oe  # "exact" is judged against current_oe's spelling
    else:
        chain.append(upstream.current_oe)
    return UpstreamCheck(chain=chain)


def upstream_row_id(state: Mapping[str, Any]) -> int:
    """Row id of a price-agent state (its ``upstream`` is an OEResolution or RowFailure)."""
    upstream: OEResolution | RowFailure = state["upstream"]
    return upstream.row_id


def prepare_from_upstream(
    ctx: AgentContext,
    upstream: OEResolution | RowFailure,
    empty_result: Callable[[int, str], BaseModel],
) -> dict[str, Any]:
    """Shared ``prepare`` node body: a failure, an empty result (row skipped) or the chain."""
    ctx.emit(upstream.row_id, "start", upstream=upstream)
    check = check_upstream(upstream)
    if check.failure is not None:
        ctx.emit(upstream.row_id, "failure", stage=check.failure.stage, error=check.failure.error)
        return {"failure": check.failure}
    if check.skip_reason is not None:
        ctx.emit(upstream.row_id, "prepare", short_circuit=check.skip_reason)
        return {"result": empty_result(upstream.row_id, check.skip_reason)}
    ctx.emit(upstream.row_id, "prepare", chain=check.chain)
    return {"chain": check.chain}


def split_chain_pages(
    hits: Iterable[ToolHit], chain: Sequence[str]
) -> tuple[list[ToolHit], list[str]]:
    """(pages that mention a chain number, ids of decoy pages that do not)."""
    keys = [normalize_part_no(n) for n in chain]
    kept: list[ToolHit] = []
    decoys: list[str] = []
    for hit in hits:
        if any(page_mentions(hit.text, k) for k in keys):
            kept.append(hit)
        elif hit.page_id not in decoys:
            decoys.append(hit.page_id)
    return kept, decoys


def non_usd(currency: str | None) -> str | None:
    """The currency code if it is not USD; a page without a currency counts as USD."""
    code = (currency or "USD").upper()
    return None if code == "USD" else code
