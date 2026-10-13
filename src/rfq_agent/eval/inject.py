"""Error injection (spec 14 experiment): make known rows wrong to test the confidence scores.

Only system results decide which rows can take an error; ground truth is never read here.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import get_args

from rfq_agent.schemas import (
    InjectionKind,
    MSRPResult,
    OEResolution,
    ResolvedRow,
    RetailResult,
)
from rfq_agent.store.runs import RowOutcomes

KINDS: tuple[InjectionKind, ...] = get_args(InjectionKind)
MSRP_FACTOR = (1.15, 1.35)


class InjectionRefused(ValueError):
    """Fewer injectable rows than asked for."""


def neighbour_part_no(part_no: str) -> str:
    """The number with its last digit bumped (9 wraps to 0); a "1" appended if it has none."""
    for i in range(len(part_no) - 1, -1, -1):
        if part_no[i].isdigit():
            return f"{part_no[:i]}{(int(part_no[i]) + 1) % 10}{part_no[i + 1 :]}"
    return part_no + "1"


def _injectable(row: ResolvedRow, outcome: RowOutcomes) -> bool:
    """Resolved, no failure, own prices, an MSRP quote and another non-outlier retail quote.

    These conditions also rule out every red gate (multiple, not_found, failure,
    all quotes outliers).
    """
    sheet = row.worksheet
    oe, msrp, retail = outcome.oe, outcome.msrp, outcome.retail
    if sheet.oe_status != "resolved" or sheet.failures or sheet.shared_oe_rows:
        return False
    if not isinstance(oe, OEResolution) or not isinstance(msrp, MSRPResult):
        return False
    if msrp.quote is None or msrp.quote.effective_price is None:
        return False
    return isinstance(retail, RetailResult) and bool(_other_sites(retail))


def _other_sites(retail: RetailResult) -> list[str]:
    """Non-outlier quotes other than the selected one."""
    if retail.selected is None:
        return []
    chosen = retail.selected.source
    return sorted(
        q.source for q in retail.quotes if q.source != chosen and q.source not in retail.outliers
    )


def inject_errors(
    rows: Sequence[ResolvedRow], outcomes: Sequence[RowOutcomes], n: int, seed: int
) -> tuple[list[RowOutcomes], dict[int, InjectionKind]]:
    """The outcomes of ``n`` injectable rows made wrong, and which row got which error.

    Injectable rows are shuffled with ``seed`` (from row_id order) and the first ``n``
    taken; the i-th gets the kind ``KINDS[i % 3]``.
    """
    by_id = {o.row_id: o for o in outcomes}
    candidates = sorted(r.line.row_id for r in rows if _injectable(r, by_id[r.line.row_id]))
    if len(candidates) < n:
        raise InjectionRefused(f"only {len(candidates)} injectable rows, {n} asked for")
    rng = random.Random(seed)
    rng.shuffle(candidates)
    injections = {row_id: KINDS[i % len(KINDS)] for i, row_id in enumerate(candidates[:n])}
    wrong = [_inject(by_id[row_id], kind, rng) for row_id, kind in injections.items()]
    return wrong, injections


def _inject(outcome: RowOutcomes, kind: InjectionKind, rng: random.Random) -> RowOutcomes:
    """One error; candidates, evidence snippets and the other paths stay as they were."""
    oe, msrp, retail = outcome.oe, outcome.msrp, outcome.retail
    if kind == "oe":
        assert isinstance(oe, OEResolution) and oe.current_oe is not None
        wrong = neighbour_part_no(oe.current_oe)
        chain = [*oe.supersession_chain[:-1], wrong]
        return outcome.model_copy(
            update={"oe": oe.model_copy(update={"current_oe": wrong, "supersession_chain": chain})}
        )
    if kind == "msrp":
        assert isinstance(msrp, MSRPResult) and msrp.quote is not None
        price = round((msrp.quote.effective_price or 0.0) * rng.uniform(*MSRP_FACTOR), 2)
        quote = msrp.quote.model_copy(
            update={"list_price": price, "sale_price": None, "effective_price": price}
        )
        return outcome.model_copy(update={"msrp": msrp.model_copy(update={"quote": quote})})
    assert isinstance(retail, RetailResult)
    site = rng.choice(_other_sites(retail))
    selected = next(q for q in retail.quotes if q.source == site)
    return outcome.model_copy(update={"retail": retail.model_copy(update={"selected": selected})})
