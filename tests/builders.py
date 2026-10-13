"""Builders for stage outcomes used by the resolution and confidence tests."""

from __future__ import annotations

from typing import Any

from rfq_agent.data_gen.truth import (
    GroundTruthRow,
    TruthInternalSpec,
    TruthListing,
    TruthMSRP,
    TruthRetail,
    TruthSpec,
)
from rfq_agent.eval.metrics import evaluate, usage_eval
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import (
    AgentUsage,
    Band,
    CostBreakdown,
    Evidence,
    MSRPResult,
    OECandidate,
    OEResolution,
    PriceQuote,
    ResolvedRow,
    RetailResult,
    RFQLine,
    RunEval,
    ShouldCostResult,
    StrategyName,
)


def line(row_id: int, part: str = "") -> RFQLine:
    part = part or f"CP-{row_id}"
    return RFQLine(
        row_id=row_id,
        customer_part_no_raw=f" {part} ",
        customer_part_no=part.replace("-", ""),
        our_part_no=None,
        description=None,
        annual_qty=None,
        customer_name=None,
    )


def oe(row_id: int, current: str | None = "NEW-3", **kw: Any) -> OEResolution:
    fields: dict[str, Any] = {
        "row_id": row_id,
        "our_part_no": f"OP-{row_id}",
        "our_part_source": "xref",
        "status": "resolved" if current else "not_found",
        "current_oe": current,
        "supersession_chain": ["OLD-1", current] if current else [],
        "candidates": [
            OECandidate(oe_part_no=current, brand="Acme", match_type="interchange", evidence=[])
        ]
        if current
        else [],
        "reasoning": f"oe reasoning {row_id}",
    }
    fields.update(kw)
    return OEResolution(**fields)


def quote(source: str, price: float, price_type: str = "retail", **kw: Any) -> PriceQuote:
    fields: dict[str, Any] = {
        "source": source,
        "price_type": price_type,
        "matched_part_no": "NEW-3",
        "list_price": price,
        "sale_price": None,
        "discount_pct": None,
        "effective_price": price,
        "in_stock": True,
        "match_quality": "exact",
        "reasoning": "",
        "evidence": [Evidence(source=source, ref="p1", snippet=f"${price}")],
    }
    fields.update(kw)
    return PriceQuote(**fields)


def msrp(row_id: int, value: float | None = 100.0, **kw: Any) -> MSRPResult:
    fields: dict[str, Any] = {
        "row_id": row_id,
        "oe_part_no": "NEW-3",
        "quote": quote("msrp_lookup", value, "msrp") if value is not None else None,
        "status": "found" if value is not None else "not_found",
        "reasoning": f"msrp reasoning {row_id}",
    }
    fields.update(kw)
    return MSRPResult(**fields)


def retail(row_id: int, prices: dict[str, float] | None = None, **kw: Any) -> RetailResult:
    prices = {"retail_1": 80.0, "retail_2": 82.0, "retail_3": 85.0} if prices is None else prices
    quotes = [quote(site, price) for site, price in prices.items()]
    fields: dict[str, Any] = {
        "row_id": row_id,
        "oe_part_no": "NEW-3",
        "quotes": quotes,
        "selected": quotes[0] if quotes else None,
        "outliers": [],
        "reasoning": f"retail reasoning {row_id}",
    }
    fields.update(kw)
    return RetailResult(**fields)


def breakdown(fob: float = 20.0, landed: float = 30.0) -> CostBreakdown:
    return CostBreakdown(
        material=5.0,
        conversion=5.0,
        overhead=2.0,
        sga=2.0,
        profit=2.0,
        packaging=4.0,
        fob=fob,
        freight=3.0,
        insurance=0.5,
        duty=4.0,
        mpf=0.5,
        brokerage=1.0,
        inland_freight=1.0,
        landed_cost=landed,
    )


def cost(row_id: int, **kw: Any) -> ShouldCostResult:
    fields: dict[str, Any] = {
        "row_id": row_id,
        "category": "thermostat",
        "material": "aluminum",
        "weight_kg": 0.4,
        "cycle_time_min": 2.0,
        "spec_source": "both_agree",
        "breakdown": breakdown(),
        "status": "estimated",
        "reasoning": f"cost reasoning {row_id}",
    }
    fields.update(kw)
    return ShouldCostResult(**fields)


def resolved_row(row_id: int, band: Band, **paths: Any) -> ResolvedRow:
    """A resolved row with the given band; paths not given are complete and consistent."""
    outcomes = {
        "oe": oe(row_id, f"OE-{row_id}"),
        "msrp": msrp(row_id),
        "retail": retail(row_id),
        "should_cost": cost(row_id),
        **paths,
    }
    row = resolve_rows(
        [line(row_id)],
        [outcomes["oe"]],
        [outcomes["msrp"]],
        [outcomes["retail"]],
        [outcomes["should_cost"]],
    )[0]
    sheet = row.worksheet.model_copy(update={"band": band, "confidence_score": 10})
    return row.model_copy(update={"worksheet": sheet})


def listing(price: float, **kw: Any) -> TruthListing:
    fields: dict[str, Any] = {
        "title_part_no": "NEW-3",
        "list_price": price,
        "sale_price": None,
        "effective_price": price,
        "in_stock": True,
        "match_quality": "exact",
        "outlier": False,
    }
    fields.update(kw)
    return TruthListing(**fields)


def truth(row_id: int, part: str = "", **kw: Any) -> GroundTruthRow:
    """Ground truth that the default builders (line, oe, msrp, retail, cost) get right."""
    part = part or f"CP-{row_id}"
    fields: dict[str, Any] = {
        "row_id": row_id,
        "customer_part_no": part.replace("-", ""),
        "customer_part_no_raw": f" {part} ",
        "tags": [],
        "our_part_no": f"OP-{row_id}",
        "our_part_reachability": "xref",
        "oe_status_expected": "resolved",
        "primary_oe": "NEW-3",
        "accepted_oes": ["NEW-3"],
        "supersession_chain": ["OLD-1", "NEW-3"],
        "shares_oe_with": [],
        "msrp": TruthMSRP(status="found", value=100.0, source_oe="NEW-3"),
        "retail": TruthRetail(
            sites={
                "retail_1": listing(80.0),
                "retail_2": listing(82.0),
                "retail_3": listing(85.0),
                "retail_4": None,
                "retail_5": None,
            },
            outlier_sites=[],
            expected_selected_site="retail_1",
        ),
        "spec": TruthSpec(
            category="thermostat", material="aluminum", weight_kg=0.4, cycle_time_min=2.0
        ),
        "internal_spec": TruthInternalSpec(material="aluminum", weight_kg=0.4),
        "external_spec_available": True,
        "cost": breakdown(),
    }
    fields.update(kw)
    return GroundTruthRow(**fields)


def _scored(row_id: int, score: int, band: Band, **paths: object) -> ResolvedRow:
    r = resolved_row(row_id, band, **paths)
    sheet = r.worksheet.model_copy(update={"confidence_score": score})
    return r.model_copy(update={"worksheet": sheet})


def run_eval(run_id: str = "run-a", strategy: StrategyName = "evidence") -> RunEval:
    """A small evaluation: row 1 usable (green), row 2 wrong MSRP (yellow), one judge agent."""
    rows = [_scored(1, 92, "green"), _scored(2, 64, "yellow", msrp=msrp(2, 150.0))]
    truths = [truth(1, primary_oe="OE-1"), truth(2, primary_oe="OE-2")]
    for t in truths:
        t.supersession_chain = ["OLD-1", t.primary_oe or ""]
    judge = AgentUsage(
        agent="confidence_judge",
        calls=3,
        errors=0,
        input_tokens=900,
        output_tokens=90,
        cost_usd=0.004,
    )
    return evaluate(
        rows,
        truths,
        run_id=run_id,
        strategy=strategy,
        truth_path="data/generated/n200_s42/ground_truth.jsonl",
        reviewed_rows=1,
        usage=usage_eval([judge], {"judge": "deepseek/deepseek-v4-pro"}, rows=2, elapsed_s=8.5),
        created_at="2026-10-09T00:00:00+00:00",
    )
