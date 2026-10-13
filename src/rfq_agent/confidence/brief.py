"""Deterministic text briefs of a resolved row for the self-assessment and judge prompts.

Briefs contain only the stage evidence and conclusions; never ground truth.
"""

from __future__ import annotations

from rfq_agent.schemas import (
    MSRPResult,
    OEResolution,
    PriceQuote,
    ResolvedRow,
    RetailResult,
    RowFailure,
    ShouldCostResult,
)

RETAIL_HEADER = "Retail quotes:"
MSRP_HEADER = "MSRP pages:"
# How the self-assessment asks its optional questions; the fake answers look for these.
OE_QUESTION = "question 1"
RETAIL_QUESTION = "question 2"
MSRP_QUESTION = "question 3"
CHAIN_PREFIX = "Supersession chain (old -> new): "


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _part_lines(row: ResolvedRow) -> list[str]:
    line = row.line
    return [
        f"Customer part number: {line.customer_part_no} "
        f"(as written: {line.customer_part_no_raw!r})",
        f"RFQ description: {line.description or '-'}",
    ]


def _oe_lines(oe: OEResolution | RowFailure) -> list[str]:
    if isinstance(oe, RowFailure):
        return [f"OE resolution failed: {oe.error}"]
    lines = ["OE candidates:"]
    for cand in oe.candidates:
        lines.append(
            f"- {cand.oe_part_no} (brand {cand.brand or '-'}, match type {cand.match_type})"
        )
        lines += [f'    evidence {e.source} {e.ref}: "{e.snippet or ""}"' for e in cand.evidence]
    if oe.supersession_chain:
        lines.append(CHAIN_PREFIX + " -> ".join(oe.supersession_chain))
    return lines


def _quote_line(quote: PriceQuote, outliers: list[str]) -> str:
    return (
        f"- {quote.source}: listed part no {quote.matched_part_no}, "
        f"list {_money(quote.list_price)}, sale {_money(quote.sale_price)}, "
        f"effective {_money(quote.effective_price)} {quote.currency}, "
        f"in stock {_yes_no(quote.in_stock)}, match quality {quote.match_quality}, "
        f"outlier {_yes_no(quote.source in outliers)}"
    )


def _retail_lines(retail: RetailResult | RowFailure) -> list[str]:
    if isinstance(retail, RowFailure):
        return [f"Retail search failed: {retail.error}"]
    if not retail.quotes:
        return [f"{RETAIL_HEADER} none"]
    return [RETAIL_HEADER, *(_quote_line(q, retail.outliers) for q in retail.quotes)]


def _msrp_lines(msrp: MSRPResult | RowFailure) -> list[str]:
    """The MSRP evidence pages and the number they were looked up for; not the price taken."""
    if isinstance(msrp, RowFailure) or msrp.quote is None:
        return []
    quote = msrp.quote
    return [
        MSRP_HEADER,
        *(
            f'- {e.source} {e.ref} (for {quote.matched_part_no}): "{e.snippet or ""}"'
            for e in quote.evidence
        ),
    ]


def self_brief(row: ResolvedRow) -> str:
    """Evidence for the key judgments (OE, retail choice, MSRP), without the conclusions."""
    return "\n".join(
        [
            *_part_lines(row),
            *_oe_lines(row.oe),
            *_retail_lines(row.retail),
            *_msrp_lines(row.msrp),
        ]
    )


def _msrp_conclusion(msrp: MSRPResult | RowFailure) -> str:
    if isinstance(msrp, RowFailure):
        return f"MSRP: failed ({msrp.error})"
    if msrp.quote is None:
        return f"MSRP: {msrp.status}"
    return (
        f"MSRP: {_money(msrp.quote.effective_price)} {msrp.quote.currency} ({msrp.status}), "
        f"taken from {msrp.quote.matched_part_no}"
    )


def _cost_conclusion(cost: ShouldCostResult | RowFailure) -> str:
    if isinstance(cost, RowFailure):
        return f"Should-cost: failed ({cost.error})"
    b = cost.breakdown
    return (
        f"Should-cost: {cost.status}; spec source {cost.spec_source}; category "
        f"{cost.category or '-'}; material {cost.material or '-'}; weight "
        f"{cost.weight_kg if cost.weight_kg is not None else '-'} kg; "
        f"FOB {_money(b.fob if b else None)}; landed cost {_money(b.landed_cost if b else None)}"
    )


def judge_brief(row: ResolvedRow) -> str:
    """All evidence plus the system's conclusions and stage reasoning."""
    ws = row.worksheet
    conclusions = [
        "System conclusions:",
        f"- OE status: {ws.oe_status or 'failed'}; current OE: {ws.current_oe or '-'}",
        f"- {_msrp_conclusion(row.msrp)}",
        f"- Selected retail: {ws.selected_retail_source or 'none'} "
        f"{_money(ws.selected_retail_price)}",
        f"- {_cost_conclusion(row.should_cost)}",
    ]
    reasoning = ["Stage reasoning:", *(f"- {k}: {v}" for k, v in ws.reasoning_summary.items())]
    return "\n".join([self_brief(row), *conclusions, *reasoning])
