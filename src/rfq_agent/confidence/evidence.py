"""Deterministic evidence signals, score, gates and bands (spec 12.2)."""

from __future__ import annotations

import statistics
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from rfq_agent.config import Settings
from rfq_agent.schemas import (
    Band,
    MSRPResult,
    OEResolution,
    ResolvedRow,
    RetailResult,
    RowFailure,
    ShouldCostResult,
)


def round_score(value: float) -> int:
    """Round half up to an integer score clamped to 0-100."""
    rounded = int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(0, min(100, rounded))


# --- signals ------------------------------------------------------------------------------


def evidence_signals(row: ResolvedRow, settings: Settings) -> dict[str, float]:
    """The six evidence signals, each in 0-1."""
    msrp = row.msrp if isinstance(row.msrp, MSRPResult) else None
    retail = row.retail if isinstance(row.retail, RetailResult) else None
    usable = _usable_prices(retail)
    return {
        "oe_match": _oe_match(row.oe, settings),
        "chain": _chain(row.oe, settings),
        "price_sources": _price_sources(msrp, usable, settings),
        "retail_dispersion": _dispersion(usable, settings),
        "msrp_retail": _msrp_retail(msrp, retail, settings),
        "spec_source": _spec_source(row.should_cost, settings),
    }


def _oe_match(oe: OEResolution | RowFailure, settings: Settings) -> float:
    levels = settings.confidence.evidence.oe_match
    if isinstance(oe, RowFailure) or not oe.candidates:
        return 0.0
    return max(levels[c.match_type] for c in oe.candidates)


def _chain(oe: OEResolution | RowFailure, settings: Settings) -> float:
    if isinstance(oe, OEResolution) and oe.chain_truncated:
        return settings.confidence.evidence.chain_truncated
    return 1.0


def _usable_prices(retail: RetailResult | None) -> list[float]:
    """Effective prices of the non-outlier retail quotes."""
    if retail is None:
        return []
    return [
        q.effective_price
        for q in retail.quotes
        if q.effective_price is not None and q.source not in retail.outliers
    ]


def _has_msrp(msrp: MSRPResult | None) -> bool:
    return msrp is not None and msrp.status != "not_found" and msrp.quote is not None


def _price_sources(msrp: MSRPResult | None, usable: list[float], settings: Settings) -> float:
    full = settings.confidence.evidence.price_sources_full
    count = int(_has_msrp(msrp)) + len(usable)
    return min(count, full) / full


def _dispersion(usable: list[float], settings: Settings) -> float:
    if len(usable) < 2:
        return settings.confidence.evidence.single_quote_dispersion
    cv = statistics.pstdev(usable) / statistics.fmean(usable)
    return max(0.0, 1.0 - cv / settings.pipeline.retail_outlier_threshold)


def _msrp_retail(msrp: MSRPResult | None, retail: RetailResult | None, settings: Settings) -> float:
    levels = settings.confidence.evidence.msrp_retail
    msrp_price = msrp.quote.effective_price if msrp and msrp.quote and _has_msrp(msrp) else None
    selected = retail.selected.effective_price if retail and retail.selected else None
    if not msrp_price or selected is None:
        return levels.missing
    ratio = selected / msrp_price
    if ratio <= 1.0:
        return levels.at_most_msrp
    if ratio <= settings.confidence.evidence.msrp_retail_suspicious_ratio:
        return levels.up_to_suspicious
    return levels.above


def _spec_source(cost: ShouldCostResult | RowFailure, settings: Settings) -> float:
    if isinstance(cost, RowFailure):
        return 0.0
    return settings.confidence.evidence.spec_source[cost.spec_source]


def evidence_score(signals: dict[str, float], settings: Settings) -> int:
    """Weighted mean of the signals, scaled to 0-100 and rounded half up."""
    weights = settings.confidence.evidence.weights
    total = sum(weights.values())
    weighted = sum(w * signals.get(name, 0.0) for name, w in weights.items())
    return round_score(100 * weighted / total)


# --- gates and bands ----------------------------------------------------------------------


# Every gate and the band whose upper limit it caps the score at. A gate missing here
# raises in ``gates()`` rather than silently getting some default band.
GATE_BANDS: dict[str, Literal["red", "yellow"]] = {
    "gate_oe_multiple": "red",
    "gate_oe_not_found": "red",
    "gate_failure": "red",
    "gate_all_outliers": "red",
    "gate_shared_disagree": "yellow",
    "gate_insufficient_data": "yellow",
}
RED_GATES = frozenset(name for name, band in GATE_BANDS.items() if band == "red")


def red_cap(settings: Settings) -> int:
    return settings.confidence.bands.yellow_min - 1


def yellow_cap(settings: Settings) -> int:
    return settings.confidence.bands.green_min - 1


def gates(row: ResolvedRow, settings: Settings) -> dict[str, int]:
    """Caps that apply to this row: gate name -> highest allowed score."""
    caps = {"red": red_cap(settings), "yellow": yellow_cap(settings)}
    return {name: caps[GATE_BANDS[name]] for name in _triggered_gates(row)}


def all_quotes_outliers(row: ResolvedRow) -> bool:
    """Retail found quotes but every one is an outlier, so there is no reference price."""
    retail = row.retail
    return isinstance(retail, RetailResult) and bool(retail.quotes) and retail.selected is None


def _triggered_gates(row: ResolvedRow) -> list[str]:
    names = []
    oe = row.oe
    if isinstance(oe, OEResolution) and oe.status == "multiple":
        names.append("gate_oe_multiple")
    if isinstance(oe, OEResolution) and oe.status == "not_found":
        names.append("gate_oe_not_found")
    if row.worksheet.failures:
        names.append("gate_failure")
    if all_quotes_outliers(row):
        names.append("gate_all_outliers")
    if row.worksheet.shared_prices_disagree:
        names.append("gate_shared_disagree")
    cost = row.should_cost
    if isinstance(cost, ShouldCostResult) and cost.status == "insufficient_data":
        names.append("gate_insufficient_data")
    return names


def apply_gates(score: int, caps: dict[str, int]) -> int:
    return min([score, *caps.values()])


def has_red_gate(caps: dict[str, int]) -> bool:
    return any(name in RED_GATES for name in caps)


def wants_self_check(caps: dict[str, int]) -> bool:
    """evidence_plus_self asks the LLM for every row without a red-band gate."""
    return not has_red_gate(caps)


def wants_judge(gated_score: int, settings: Settings) -> bool:
    """evidence_plus_judge reviews rows whose gated evidence score is in ``judge_band``."""
    low, high = settings.confidence.judge_band
    return low <= gated_score <= high


def band_for(score: int, settings: Settings) -> Band:
    bands = settings.confidence.bands
    if score >= bands.green_min:
        return "green"
    if score >= bands.yellow_min:
        return "yellow"
    return "red"
