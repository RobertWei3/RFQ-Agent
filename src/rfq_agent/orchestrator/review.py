"""Manual review of red rows (spec 13): decisions, overlays on the worksheet, manual OE."""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Sequence
from typing import TYPE_CHECKING

from rfq_agent.agents.oe_msrp import resolve_msrp
from rfq_agent.agents.retail_price import resolve_retail
from rfq_agent.agents.should_cost import resolve_should_cost
from rfq_agent.schemas import (
    Evidence,
    OECandidate,
    OEResolution,
    ResolvedRow,
    ReviewAction,
    ReviewDecision,
    ReviewEntry,
    ReviewStatus,
    RowFailure,
)
from rfq_agent.store.runs import RowOutcomes

if TYPE_CHECKING:
    from rfq_agent.orchestrator.context import RunContext

MANUAL_SOURCE = "manual_review"
MANUAL_PRICE_REASON = "price entered in manual review"
MANUAL_OE_REASONING = "OE number entered in manual review"

_STATUS: dict[ReviewAction, ReviewStatus] = {
    "accept": "accepted",
    "modify_oe": "modified",
    "modify_price": "modified",
    "reject": "rejected",
}


def apply_reviews(row: ResolvedRow, entries: Sequence[ReviewEntry]) -> ResolvedRow:
    """The row as the worksheet shows it after its review decisions (latest wins).

    Only worksheet columns change; the agents' own results stay as they were.
    """
    if not entries:
        return row
    update: dict[str, object] = {"review_status": _STATUS[entries[-1].action]}
    priced = [e for e in entries if e.action == "modify_price"]
    msrp = next((e.msrp_after for e in reversed(priced) if e.msrp_after is not None), None)
    retail = next((e.retail_after for e in reversed(priced) if e.retail_after is not None), None)
    if msrp is not None:
        update |= {"msrp": msrp, "msrp_status": "found"}
    if retail is not None:
        update |= {"selected_retail_price": retail, "selected_retail_source": MANUAL_SOURCE}
    if priced:
        update["review_reasons"] = [*row.worksheet.review_reasons, MANUAL_PRICE_REASON]
    return row.model_copy(update={"worksheet": row.worksheet.model_copy(update=update)})


def manual_oe_resolution(
    original: OEResolution | RowFailure, oe_number: str, run_id: str
) -> OEResolution:
    """A resolved OE from the number the reviewer typed; our part number is kept."""
    current = unicodedata.normalize("NFKC", oe_number).strip().upper()
    known = original if isinstance(original, OEResolution) else None
    return OEResolution(
        row_id=original.row_id,
        our_part_no=known.our_part_no if known else None,
        our_part_source=known.our_part_source if known else "none",
        status="resolved",
        current_oe=current,
        supersession_chain=[current],
        candidates=[
            OECandidate(
                oe_part_no=current,
                brand=None,
                match_type="manual",
                evidence=[Evidence(source=MANUAL_SOURCE, ref=f"{run_id}:{original.row_id}")],
            )
        ],
        reasoning=MANUAL_OE_REASONING,
    )


async def record_decision(ctx: RunContext, row: ResolvedRow, decision: ReviewDecision) -> None:
    """Write the decision to ``review_log``; a new OE first reruns the downstream agents.

    Idempotent per row: a row that already has an entry is left alone.
    """
    run_id, rid = ctx.run_id, row.line.row_id
    if any(e.row_id == rid for e in ctx.store.reviews(run_id)):
        return
    sheet = row.worksheet
    entry = ReviewEntry(
        row_id=rid,
        action=decision.action,
        oe_before=sheet.current_oe,
        msrp_before=sheet.msrp,
        retail_before=sheet.selected_retail_price,
        note=decision.note,
        snapshot=row,
    )
    if decision.action == "modify_oe" and decision.oe:
        manual = await rerun_with_manual_oe(ctx, row, decision.oe)
        entry.oe_after = manual.current_oe
    if decision.action == "modify_price":
        entry.msrp_after, entry.retail_after = decision.msrp, decision.retail
    ctx.store.log_review(run_id, entry)


async def rerun_with_manual_oe(ctx: RunContext, row: ResolvedRow, oe_number: str) -> OEResolution:
    """Price and cost one row again with the reviewer's OE number (spec 13).

    A replay after a crash finds the outcomes already saved for this OE and reuses them.
    """
    manual = manual_oe_resolution(row.oe, oe_number, ctx.run_id)
    saved = ctx.store.outcomes(ctx.run_id, [row.line.row_id])
    if saved and saved[0].manual_oe and saved[0].oe == manual:
        return manual
    msrp, retail, cost = await asyncio.gather(
        resolve_msrp(manual, ctx.msrp),
        resolve_retail(manual, ctx.retail),
        resolve_should_cost(row.line, manual, ctx.cost),
    )
    ctx.store.save_outcomes(
        ctx.run_id,
        [
            RowOutcomes(
                row_id=row.line.row_id,
                oe=manual,
                msrp=msrp,
                retail=retail,
                should_cost=cost,
                manual_oe=True,
            )
        ],
    )
    return manual
