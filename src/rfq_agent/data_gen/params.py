"""Generator parameters: difficulty ratios (spec section 6.3) and price/coverage settings."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Difficulty(StrEnum):
    """Difficulty tags attached to RFQ rows. Ratios are over all rows."""

    SUPERSESSION = "supersession"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    OE_NOT_FOUND = "oe_not_found"
    RETAIL_MISSING = "retail_missing"
    RETAIL_OUTLIER = "retail_outlier"
    RETAIL_VARIANT_SPELLING = "retail_variant_spelling"
    INTERNAL_SPEC_MISSING = "internal_spec_missing"
    SPEC_CONFLICT = "spec_conflict"
    DIRTY_INPUT = "dirty_input"
    # Additional MSRP cases agreed for P1 (not listed in section 6.3).
    MSRP_DISCONTINUED = "msrp_discontinued"
    MSRP_FALLBACK = "msrp_fallback"
    MSRP_MISSING = "msrp_missing"


SECTION_6_3 = (
    Difficulty.SUPERSESSION,
    Difficulty.ONE_TO_MANY,
    Difficulty.MANY_TO_ONE,
    Difficulty.OE_NOT_FOUND,
    Difficulty.RETAIL_MISSING,
    Difficulty.RETAIL_OUTLIER,
    Difficulty.RETAIL_VARIANT_SPELLING,
    Difficulty.INTERNAL_SPEC_MISSING,
    Difficulty.SPEC_CONFLICT,
    Difficulty.DIRTY_INPUT,
)


def default_ratios() -> dict[Difficulty, float]:
    return {
        Difficulty.SUPERSESSION: 0.15,
        Difficulty.ONE_TO_MANY: 0.10,
        Difficulty.MANY_TO_ONE: 0.10,
        Difficulty.OE_NOT_FOUND: 0.08,
        Difficulty.RETAIL_MISSING: 0.15,
        Difficulty.RETAIL_OUTLIER: 0.10,
        Difficulty.RETAIL_VARIANT_SPELLING: 0.20,
        Difficulty.INTERNAL_SPEC_MISSING: 0.15,
        Difficulty.SPEC_CONFLICT: 0.05,
        Difficulty.DIRTY_INPUT: 0.05,
        Difficulty.MSRP_DISCONTINUED: 0.05,
        Difficulty.MSRP_MISSING: 0.03,
    }


class PriceRange(BaseModel):
    low: float = Field(gt=0)
    high: float = Field(gt=0)


class GenParams(BaseModel):
    n_parts: int = Field(default=200, ge=20)
    seed: int = 42
    customer_name: str = "Meridian Fleet Supply"
    ratios: dict[Difficulty, float] = Field(default_factory=default_ratios)

    # Share of supersession rows whose current OE has no MSRP page (fallback to an older number).
    msrp_fallback_share: float = Field(default=0.30, ge=0, le=1)

    # Our-part reachability: given in the RFQ, else found via customer_xref, else unreachable.
    our_part_given_ratio: float = Field(default=0.25, ge=0, le=1)
    our_part_xref_ratio: float = Field(default=0.50, ge=0, le=1)
    distractor_part_ratio: float = Field(default=0.30, ge=0)

    msrp_ranges: dict[str, PriceRange] = Field(
        default_factory=lambda: {
            "thermostat": PriceRange(low=20, high=90),
            "water_pump": PriceRange(low=60, high=320),
        }
    )
    retail_list_of_msrp: tuple[float, float] = (0.55, 0.95)
    landed_of_retail: tuple[float, float] = (0.25, 0.45)
    discount_share: float = 0.30
    discount_pct: tuple[float, float] = (0.05, 0.25)
    out_of_stock_share: float = 0.10
    description_share: float = 0.50
    external_spec_share: float = Field(
        default=0.85, description="Share of found OEs with an external spec page (when optional)."
    )

    def quota(self, difficulty: Difficulty) -> int:
        return round(self.ratios.get(difficulty, 0.0) * self.n_parts)
