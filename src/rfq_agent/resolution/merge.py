"""Merge oe_interchange, oe_msrp, retail_price and should_cost outcomes per row (spec 12.1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import (
    SNIPPET_MAX_CHARS,
    MSRPResult,
    OEResolution,
    ResolvedRow,
    RetailResult,
    RFQLine,
    RowFailure,
    ShouldCostResult,
    WorksheetRow,
)

MULTIPLE_REASON = "OE not uniquely resolved; pending manual selection"

OEOutcome = OEResolution | RowFailure
MSRPOutcome = MSRPResult | RowFailure
RetailOutcome = RetailResult | RowFailure
CostOutcome = ShouldCostResult | RowFailure


class ResolutionInputError(ValueError):
    """The four outcome sets and the RFQ lines do not cover the same rows."""


@dataclass
class _Sharing:
    """How one row takes part in a shared-OE group (many-to-one)."""

    others: list[int] = field(default_factory=list)
    msrp_from: int | None = None
    retail_from: int | None = None
    disagree: list[int] = field(default_factory=list)


def resolve_rows(
    lines: Sequence[RFQLine],
    oe: Sequence[OEOutcome],
    msrp: Sequence[MSRPOutcome],
    retail: Sequence[RetailOutcome],
    should_cost: Sequence[CostOutcome],
) -> list[ResolvedRow]:
    """One ``ResolvedRow`` per RFQ line, ordered by row_id."""
    _check_rows(lines, oe=oe, msrp=msrp, retail=retail, should_cost=should_cost)
    oe_by = {o.row_id: o for o in oe}
    msrp_by = {o.row_id: o for o in msrp}
    retail_by = {o.row_id: o for o in retail}
    cost_by = {o.row_id: o for o in should_cost}
    sharing = _shared_groups(oe, msrp_by, retail_by)
    rows = []
    for item in sorted(lines, key=lambda x: x.row_id):
        rid = item.row_id
        share = sharing.get(rid, _Sharing())
        rows.append(
            _row(
                item,
                oe_by[rid],
                msrp_by[_or_self(share.msrp_from, rid)],
                retail_by[_or_self(share.retail_from, rid)],
                cost_by[rid],
                share,
            )
        )
    return rows


def _check_rows(
    lines: Sequence[RFQLine],
    **paths: Sequence[OEOutcome]
    | Sequence[MSRPOutcome]
    | Sequence[RetailOutcome]
    | Sequence[CostOutcome],
) -> None:
    wanted = {item.row_id for item in lines}
    problems = []
    for name, outcomes in paths.items():
        have = {o.row_id for o in outcomes}
        if have != wanted:
            problems.append(
                f"{name}: missing {sorted(wanted - have)}; extra {sorted(have - wanted)}"
            )
    if problems:
        raise ResolutionInputError("row_ids differ from the RFQ lines: " + " | ".join(problems))


def _shared_groups(
    oe: Sequence[OEOutcome],
    msrp_by: dict[int, MSRPOutcome],
    retail_by: dict[int, RetailOutcome],
) -> dict[int, _Sharing]:
    """Group resolved rows by normalized current OE and pick a representative per path."""
    groups: defaultdict[str, list[int]] = defaultdict(list)
    for o in oe:
        if isinstance(o, OEResolution) and o.status == "resolved" and o.current_oe:
            groups[normalize_part_no(o.current_oe)].append(o.row_id)
    sharing: dict[int, _Sharing] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort()
        msrp_rep = _representative(members, msrp_by)
        retail_rep = _representative(members, retail_by)
        disagree = members if _prices_disagree(members, msrp_by, retail_by) else []
        for rid in members:
            sharing[rid] = _Sharing(
                others=[m for m in members if m != rid],
                msrp_from=msrp_rep if msrp_rep != rid else None,
                retail_from=retail_rep if retail_rep != rid else None,
                disagree=disagree,
            )
    return sharing


def _representative(
    members: list[int], by_row: dict[int, MSRPOutcome] | dict[int, RetailOutcome]
) -> int:
    """Lowest row_id whose outcome on this path is not a failure (else the lowest)."""
    for rid in members:
        if not isinstance(by_row[rid], RowFailure):
            return rid
    return members[0]


def _prices_disagree(
    members: list[int],
    msrp_by: dict[int, MSRPOutcome],
    retail_by: dict[int, RetailOutcome],
) -> bool:
    msrp_keys = {_msrp_key(m) for rid in members if isinstance(m := msrp_by[rid], MSRPResult)}
    retail_keys = {
        _retail_key(r) for rid in members if isinstance(r := retail_by[rid], RetailResult)
    }
    return len(msrp_keys) > 1 or len(retail_keys) > 1


def _msrp_key(result: MSRPResult) -> tuple[str, float | None]:
    return result.status, result.quote.effective_price if result.quote else None


def _retail_key(result: RetailResult) -> tuple[str | None, float | None]:
    selected = result.selected
    return (selected.source, selected.effective_price) if selected else (None, None)


def _row(
    item: RFQLine,
    oe: OEOutcome,
    msrp: MSRPOutcome,
    retail: RetailOutcome,
    cost: CostOutcome,
    share: _Sharing,
) -> ResolvedRow:
    resolved = oe if isinstance(oe, OEResolution) else None
    # Price agents only look up uniquely resolved OEs; other rows show empty price columns.
    priced = resolved is not None and resolved.status == "resolved"
    msrp_ok = msrp if priced and isinstance(msrp, MSRPResult) else None
    retail_ok = retail if priced and isinstance(retail, RetailResult) else None
    cost_ok = cost if isinstance(cost, ShouldCostResult) else None
    selected = retail_ok.selected if retail_ok else None
    breakdown = cost_ok.breakdown if cost_ok else None
    worksheet = WorksheetRow(
        row_id=item.row_id,
        customer_part_no_raw=item.customer_part_no_raw,
        customer_part_no=item.customer_part_no,
        our_part_no=resolved.our_part_no if resolved else item.our_part_no,
        oe_status=resolved.status if resolved else None,
        current_oe=resolved.current_oe if resolved else None,
        supersession_chain=resolved.supersession_chain if resolved else [],
        oe_candidates=_candidates(resolved),
        shared_oe_rows=share.others,
        price_source_row=share.msrp_from if share.msrp_from is not None else share.retail_from,
        shared_prices_disagree=bool(share.disagree),
        msrp=msrp_ok.quote.effective_price if msrp_ok and msrp_ok.quote else None,
        msrp_status=msrp_ok.status if msrp_ok else None,
        retail_prices={q.source: q.effective_price for q in retail_ok.quotes} if retail_ok else {},
        selected_retail_price=selected.effective_price if selected else None,
        selected_retail_source=selected.source if selected else None,
        retail_outliers=list(retail_ok.outliers) if retail_ok else [],
        should_cost_fob=breakdown.fob if breakdown else None,
        landed_cost=breakdown.landed_cost if breakdown else None,
        review_reasons=_review_reasons(oe, msrp, retail, cost, share),
        failures=_failures(oe, msrp, retail, cost),
        reasoning_summary=_reasoning_summary(item.row_id, oe, msrp, retail, cost, share),
    )
    return ResolvedRow(
        line=item, worksheet=worksheet, oe=oe, msrp=msrp, retail=retail, should_cost=cost
    )


def _or_self(source_row: int | None, row_id: int) -> int:
    return row_id if source_row is None else source_row


def _candidates(resolved: OEResolution | None) -> list[str]:
    """Current OE of each grounded candidate; ``llm_inferred`` ones stay in the detail only."""
    if resolved is None or resolved.status != "multiple":
        return []
    return [c.oe_part_no for c in resolved.candidates if c.match_type != "llm_inferred"]


def _review_reasons(
    oe: OEOutcome,
    msrp: MSRPOutcome,
    retail: RetailOutcome,
    cost: CostOutcome,
    share: _Sharing,
) -> list[str]:
    """Reasons a person should look at this row, straight from the stage outcomes."""
    if isinstance(oe, RowFailure):
        return [f"OE resolution failed: {oe.error}"]
    reasons = _oe_reasons(oe)
    if oe.status == "resolved":
        reasons += _msrp_reasons(msrp) + _retail_reasons(retail)
    reasons += _cost_reasons(cost)
    if share.disagree:
        rows = ", ".join(str(r) for r in share.disagree)
        reasons.append(f"shared-OE rows disagree on prices (rows {rows})")
    return reasons


def _oe_reasons(oe: OEResolution) -> list[str]:
    if oe.status == "multiple":
        return [MULTIPLE_REASON]
    if oe.status == "not_found":
        return ["no OE number found"]
    return []


def _msrp_reasons(msrp: MSRPOutcome) -> list[str]:
    if isinstance(msrp, RowFailure):
        return [f"MSRP not retrieved: {msrp.error}"]
    if msrp.status == "not_found" or msrp.quote is None:
        return ["no MSRP found"]
    reasons = []
    if msrp.status == "discontinued":
        reasons.append("MSRP page marks the part discontinued")
    used = msrp.quote.matched_part_no
    if msrp.oe_part_no and normalize_part_no(used) != normalize_part_no(msrp.oe_part_no):
        reasons.append(f"MSRP taken from superseded number {used}")
    return reasons


def _retail_reasons(retail: RetailOutcome) -> list[str]:
    if isinstance(retail, RowFailure):
        return [f"retail prices not retrieved: {retail.error}"]
    if not retail.quotes:
        return ["no retail quotes found"]
    if retail.selected is None:
        return ["no reliable retail reference: every quote is an outlier"]
    if retail.outliers:
        return [f"retail outliers: {', '.join(retail.outliers)}"]
    return []


def _cost_reasons(cost: CostOutcome) -> list[str]:
    if isinstance(cost, RowFailure):
        return [f"should-cost not computed: {cost.error}"]
    if cost.status == "insufficient_data":
        return ["should-cost: insufficient data"]
    if cost.spec_source == "conflict":
        return ["internal and external specs conflict (internal values used)"]
    return []


def _failures(
    oe: OEOutcome, msrp: MSRPOutcome, retail: RetailOutcome, cost: CostOutcome
) -> list[str]:
    """``path:stage:error`` per failed path; an OE failure is the only root cause reported."""
    if isinstance(oe, RowFailure):
        return [f"oe:{oe.stage}:{oe.error}"]
    paths = {"msrp": msrp, "retail": retail, "should_cost": cost}
    return [f"{name}:{o.stage}:{o.error}" for name, o in paths.items() if isinstance(o, RowFailure)]


def _reasoning_summary(
    row_id: int,
    oe: OEOutcome,
    msrp: MSRPOutcome,
    retail: RetailOutcome,
    cost: CostOutcome,
    share: _Sharing,
) -> dict[str, str]:
    summary = {
        "oe": _summary(oe),
        "msrp": _summary(msrp),
        "retail": _summary(retail),
        "should_cost": _summary(cost),
    }
    if share.others:
        summary["shared"] = _sharing_note(row_id, share)
    return summary


def _sharing_note(row_id: int, share: _Sharing) -> str:
    rows = ", ".join(str(r) for r in share.others)
    msrp_from = _or_self(share.msrp_from, row_id)
    retail_from = _or_self(share.retail_from, row_id)
    if msrp_from == retail_from:
        return f"shares OE with row {rows}; prices taken from row {msrp_from}"
    return (
        f"shares OE with row {rows}; MSRP taken from row {msrp_from}, "
        f"retail taken from row {retail_from}"
    )


def _summary(outcome: OEOutcome | MSRPOutcome | RetailOutcome | CostOutcome) -> str:
    text = outcome.error if isinstance(outcome, RowFailure) else outcome.reasoning
    return text[:SNIPPET_MAX_CHARS]
