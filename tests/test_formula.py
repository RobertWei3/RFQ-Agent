from __future__ import annotations

import pytest

from rfq_agent.config import CategoryCost, CostModel
from rfq_agent.costing.formula import compute_breakdown
from rfq_agent.schemas import CostBreakdown


@pytest.fixture
def model() -> CostModel:
    return CostModel(
        scrap_rate=0.05,
        labor_rate_per_hr=10,
        machine_rate_per_hr=20,
        overhead_rate=1.0,
        sga_rate=0.10,
        profit_rate=0.10,
        insurance_rate=0.01,
        mfn_duty_rate=0.02,
        additional_tariff_rate=0.25,
        mpf_rate=0.003,
        brokerage_per_entry=200,
        units_per_shipment=1000,
        categories={
            "thermostat": CategoryCost(
                cycle_time_min=6, packaging=0.5, freight_per_unit=0.4, inland_freight_per_unit=0.2
            )
        },
    )


# Worked example: 0.4 kg at $5.00/kg, thermostat (6 min cycle, $30/h labor + machine).
# Every expected value below is computed by hand from the spec section 11.4 formula.
WORKED_EXAMPLE = {
    "material": 2.1,  # 0.4 * 5.00 * 1.05
    "conversion": 3.0,  # 6 / 60 * (10 + 20)
    "overhead": 3.0,  # 3.0 * 1.0
    "sga": 0.81,  # (2.1 + 3.0 + 3.0) * 0.10
    "profit": 0.891,  # (8.1 + 0.81) * 0.10
    "packaging": 0.5,  # category value
    "fob": 10.301,  # 2.1 + 3.0 + 3.0 + 0.81 + 0.891 + 0.5
    "freight": 0.4,  # category value
    "insurance": 0.10701,  # (10.301 + 0.4) * 0.01
    "duty": 2.78127,  # 10.301 * (0.02 + 0.25), FOB basis
    "mpf": 0.030903,  # 10.301 * 0.003
    "brokerage": 0.2,  # 200 / 1000
    "inland_freight": 0.2,  # category value
    "landed_cost": 14.020183,  # 10.301 + 0.4 + 0.10701 + 2.78127 + 0.030903 + 0.2 + 0.2
}


@pytest.mark.parametrize(("term", "expected"), WORKED_EXAMPLE.items())
def test_each_term_of_the_worked_example(model: CostModel, term: str, expected: float) -> None:
    b = compute_breakdown(
        weight_kg=0.4, material_price_per_kg=5.0, category="thermostat", cost_model=model
    )
    assert getattr(b, term) == pytest.approx(expected)


def test_worked_example_covers_every_breakdown_term() -> None:
    assert set(WORKED_EXAMPLE) == set(CostBreakdown.model_fields) - {"schema_version"}


def test_part_cycle_time_overrides_category(model: CostModel) -> None:
    b = compute_breakdown(
        weight_kg=0,
        material_price_per_kg=0,
        category="thermostat",
        cost_model=model,
        cycle_time_min=12,
    )
    assert b.material == 0
    assert b.conversion == pytest.approx(6.0)


def test_invalid_inputs(model: CostModel) -> None:
    with pytest.raises(ValueError, match="category"):
        compute_breakdown(weight_kg=1, material_price_per_kg=1, category="pump", cost_model=model)
    with pytest.raises(ValueError, match="non-negative"):
        compute_breakdown(
            weight_kg=-1, material_price_per_kg=1, category="thermostat", cost_model=model
        )
    bad = model.model_copy(update={"units_per_shipment": 0})
    with pytest.raises(ValueError, match="units_per_shipment"):
        compute_breakdown(
            weight_kg=1, material_price_per_kg=1, category="thermostat", cost_model=bad
        )
