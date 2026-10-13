"""Ground-truth records for evaluation.

Only the generator and the evaluation code may import this module. Tools,
agents and the FakeLLM must never read ground truth.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from rfq_agent.schemas import CostBreakdown

SiteName = str
Reachability = Literal["given", "xref", "unreachable"]


class TruthListing(BaseModel):
    """One retail site's listing for the primary OE."""

    title_part_no: str
    list_price: float
    sale_price: float | None
    effective_price: float
    in_stock: bool
    match_quality: Literal["exact", "normalized"]
    outlier: bool


class TruthMSRP(BaseModel):
    status: Literal["found", "not_found", "discontinued"]
    value: float | None
    source_oe: str | None


class TruthRetail(BaseModel):
    sites: dict[SiteName, TruthListing | None]
    outlier_sites: list[SiteName]
    expected_selected_site: SiteName | None


class TruthSpec(BaseModel):
    category: str
    material: str
    weight_kg: float
    cycle_time_min: float


class TruthInternalSpec(BaseModel):
    """What the internal database holds (may be incomplete)."""

    material: str | None
    weight_kg: float | None


class GroundTruthRow(BaseModel):
    row_id: int
    customer_part_no: str
    customer_part_no_raw: str
    tags: list[str]
    our_part_no: str
    our_part_reachability: Reachability
    oe_status_expected: Literal["resolved", "multiple", "not_found"]
    primary_oe: str | None
    accepted_oes: list[str]
    supersession_chain: list[str]
    shares_oe_with: list[int]
    msrp: TruthMSRP
    retail: TruthRetail
    spec: TruthSpec
    internal_spec: TruthInternalSpec
    external_spec_available: bool
    cost: CostBreakdown
    source_row_id: int | None = None
