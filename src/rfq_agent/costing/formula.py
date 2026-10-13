"""Should-cost and landed-cost formula (spec section 11.4).

All rates come from ``config/cost_model.yaml`` and are illustrative only.
"""

from __future__ import annotations

from rfq_agent.config import CostModel
from rfq_agent.schemas import CostBreakdown


def compute_breakdown(
    *,
    weight_kg: float,
    material_price_per_kg: float,
    category: str,
    cost_model: CostModel,
    cycle_time_min: float | None = None,
) -> CostBreakdown:
    """Compute the full cost stack for one unit.

    ``cycle_time_min`` is the part-level cycle time; when ``None`` the category
    default from the cost model is used.
    """
    if weight_kg < 0 or material_price_per_kg < 0:
        raise ValueError("weight_kg and material_price_per_kg must be non-negative")
    if category not in cost_model.categories:
        raise ValueError(f"Unknown category {category!r}")
    if cost_model.units_per_shipment <= 0:
        raise ValueError("cost_model.units_per_shipment must be positive")

    cat = cost_model.categories[category]
    cycle = cat.cycle_time_min if cycle_time_min is None else cycle_time_min
    m = cost_model

    material = weight_kg * material_price_per_kg * (1 + m.scrap_rate)
    conversion = cycle / 60 * (m.labor_rate_per_hr + m.machine_rate_per_hr)
    overhead = conversion * m.overhead_rate
    sga = (material + conversion + overhead) * m.sga_rate
    profit = (material + conversion + overhead + sga) * m.profit_rate
    packaging = cat.packaging
    fob = material + conversion + overhead + sga + profit + packaging

    freight = cat.freight_per_unit
    insurance = (fob + freight) * m.insurance_rate
    duty = fob * (m.mfn_duty_rate + m.additional_tariff_rate)
    mpf = fob * m.mpf_rate
    brokerage = m.brokerage_per_entry / m.units_per_shipment
    inland_freight = cat.inland_freight_per_unit
    landed_cost = fob + freight + insurance + duty + mpf + brokerage + inland_freight

    return CostBreakdown(
        material=material,
        conversion=conversion,
        overhead=overhead,
        sga=sga,
        profit=profit,
        packaging=packaging,
        fob=fob,
        freight=freight,
        insurance=insurance,
        duty=duty,
        mpf=mpf,
        brokerage=brokerage,
        inland_freight=inland_freight,
        landed_cost=landed_cost,
    )
