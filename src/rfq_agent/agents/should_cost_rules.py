"""Deterministic should-cost spec rules (spec section 11.4).

Pure functions so the business rules can be tested and adjusted without the
subgraph, tools or an LLM.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from rfq_agent.agents.common import page_mentions
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import Evidence, SpecSource, weakest_spec_source

T = TypeVar("T", str, float)

EXTERNAL_SPEC_SOURCE = "external_spec"
OEM_CATALOG_SOURCE = "oem_catalog"
OTHER_MATERIAL = "other"

KG_PER_LB = 0.45359237
_GRAM = Decimal("0.001")
_FLOAT_SLACK = 1e-9  # 1.05 kg vs 1.00 kg must count as exactly 5%
KG_PER_UNIT = {"kg": 1.0, "kgs": 1.0, "g": 0.001, "lb": KG_PER_LB, "lbs": KG_PER_LB}
# "<number> <unit>" on one line ("412 g", "1,200 g", "0.4kg"); digits glued to part
# numbers do not count. Shared with the FakeLLM handler.
WEIGHT_PATTERN = re.compile(
    r"(?<![\w.,\-/])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)[ \t]*([A-Za-z]+)\b"
)


class SpecPageFacts(BaseModel):
    """Facts the LLM extracts from one external spec page or OEM catalog page."""

    page_id: str
    material_text: str | None = Field(
        default=None, description="Material phrase exactly as written on the page."
    )
    material_key: str | None = Field(
        default=None, description="The known material key this phrase means, or 'other'."
    )
    weight_value: float | None = Field(
        default=None, description="Weight number exactly as written, without the unit."
    )
    weight_unit: str | None = Field(
        default=None, description="Weight unit exactly as written, e.g. kg, g, lb."
    )
    category_text: str | None = Field(
        default=None, description="Product type phrase exactly as written (catalog pages)."
    )


PageKind = Literal["spec", "catalog"]


@dataclass(frozen=True)
class SpecPage:
    kind: PageKind
    text: str


@dataclass
class ExternalField(Generic[T]):
    """One spec field as published externally, merged over every grounded page.

    ``value`` is set only when every grounded page agrees; ``disagree`` marks
    pages that contradict each other (the field is then unusable).
    """

    value: T | None = None
    disagree: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    _seen: list[T] = field(default_factory=list, repr=False)

    def add(self, value: T, evidence: Evidence) -> None:
        self._seen.append(value)
        self.evidence.append(evidence)

    def merge(self, same: Callable[[T, T], bool]) -> None:
        """Pages are visited in page_id order, so the first value is the smallest page_id's."""
        if not self._seen:
            return
        first = self._seen[0]
        self.disagree = not all(same(first, other) for other in self._seen[1:])
        self.value = None if self.disagree else first


@dataclass
class RejectedFact:
    """An extracted fact that deterministic checks rejected (kept for the trace)."""

    page_id: str
    reason: str


@dataclass
class ExternalSpec:
    material: ExternalField[str] = field(default_factory=ExternalField)
    weight_kg: ExternalField[float] = field(default_factory=ExternalField)
    category: ExternalField[str] = field(default_factory=ExternalField)
    rejected: list[RejectedFact] = field(default_factory=list)


def verify_spec_pages(
    facts: Sequence[SpecPageFacts],
    pages: Mapping[str, SpecPage],
    *,
    current_oe: str,
    known_materials: Sequence[str],
    tolerance: float,
) -> ExternalSpec:
    """Keep only extracted facts grounded in the page text and merge them per field."""
    spec = ExternalSpec()
    oe_key = normalize_part_no(current_oe)
    for fact in sorted(facts, key=lambda f: f.page_id):
        page = pages.get(fact.page_id)
        if page is None:
            spec.rejected.append(RejectedFact(fact.page_id, "page_id not in the search results"))
            continue
        if not page_mentions(page.text, oe_key):
            spec.rejected.append(RejectedFact(fact.page_id, f"page does not mention {current_oe}"))
            continue
        if page.kind == "catalog":
            _add_category(spec, fact, page.text)
        else:
            _add_spec_fields(spec, fact, page.text, known_materials)
    spec.material.merge(values_agree)
    spec.category.merge(values_agree)
    spec.weight_kg.merge(partial(weights_agree, tolerance=tolerance))
    return spec


def values_agree(reference: str, other: str) -> bool:
    """Material keys and categories agree only when identical."""
    return reference == other


def weights_agree(reference: float, other: float, tolerance: float) -> bool:
    """True if ``other`` is within ``tolerance`` (relative) of ``reference``."""
    return abs(other - reference) <= tolerance * reference + _FLOAT_SLACK


# --- internal vs external -> values used and spec_source ------------------------------


@dataclass
class ReconciledSpec:
    material: str | None  # value used for costing (internal wins on conflict)
    weight_kg: float | None
    material_source: SpecSource
    weight_source: SpecSource
    spec_source: SpecSource


def reconcile_spec(
    *,
    internal_material: str | None,
    internal_weight_kg: float | None,
    external: ExternalSpec,
    tolerance: float,
) -> ReconciledSpec:
    """Per-field internal vs external decision, then the row's weakest source."""
    material, material_source = _reconcile_field(internal_material, external.material, values_agree)
    weight, weight_source = _reconcile_field(
        internal_weight_kg, external.weight_kg, partial(weights_agree, tolerance=tolerance)
    )
    spec_source = weakest_spec_source((material_source, weight_source))
    return ReconciledSpec(material, weight, material_source, weight_source, spec_source)


def _reconcile_field(
    internal: T | None, external: ExternalField[T], same: Callable[[T, T], bool]
) -> tuple[T | None, SpecSource]:
    if internal is not None:
        if external.disagree:
            return internal, "conflict"
        if external.value is None:
            return internal, "internal"
        return internal, "both_agree" if same(internal, external.value) else "conflict"
    if external.value is not None:
        return external.value, "external"
    return None, "missing"


def category_from_text(text: str) -> str | None:
    """Map a product-type phrase to a cost-model category (spec 11.4); None if unknown."""
    lowered = text.lower()
    if "thermostat" in lowered:
        return "thermostat"
    if "pump" in lowered:
        return "water_pump"
    return None


def _add_category(spec: ExternalSpec, fact: SpecPageFacts, text: str) -> None:
    phrase = fact.category_text
    if not phrase:
        return
    if phrase.lower() not in text.lower():
        spec.rejected.append(RejectedFact(fact.page_id, f"category {phrase!r} not on the page"))
        return
    category = category_from_text(phrase)
    if category is None:
        spec.rejected.append(RejectedFact(fact.page_id, f"category {phrase!r} not recognized"))
        return
    spec.category.add(category, _evidence(fact.page_id, phrase, OEM_CATALOG_SOURCE))


def _add_spec_fields(
    spec: ExternalSpec, fact: SpecPageFacts, text: str, known_materials: Sequence[str]
) -> None:
    material = _material_key(fact, text, known_materials, spec.rejected)
    if material is not None:
        spec.material.add(material, _evidence(fact.page_id, fact.material_text or ""))
    weight = _weight_kg(fact, text, spec.rejected)
    if weight is not None:
        spec.weight_kg.add(
            weight, _evidence(fact.page_id, f"{fact.weight_value:g} {fact.weight_unit}")
        )


def _material_key(
    fact: SpecPageFacts, text: str, known: Sequence[str], rejected: list[RejectedFact]
) -> str | None:
    """The material key if the phrase is on the page; unknown keys become ``other``."""
    if not fact.material_text:
        return None
    if fact.material_text.lower() not in text.lower():
        rejected.append(
            RejectedFact(fact.page_id, f"material {fact.material_text!r} not found on the page")
        )
        return None
    return fact.material_key if fact.material_key in known else OTHER_MATERIAL


def _weight_kg(fact: SpecPageFacts, text: str, rejected: list[RejectedFact]) -> float | None:
    """The fact's weight in kg if "<number> <unit>" is on the page, else None (rejected)."""
    if fact.weight_value is None or fact.weight_unit is None:
        return None
    unit = fact.weight_unit.lower()
    reason = None
    if unit not in KG_PER_UNIT:
        reason = f"weight unit {fact.weight_unit!r} is not kg / g / lb"
    elif fact.weight_value <= 0:
        reason = f"weight {fact.weight_value:g} is not positive"
    elif not _weight_on_page(fact.weight_value, unit, text):
        reason = f"weight '{fact.weight_value:g} {fact.weight_unit}' not found on the page"
    if reason is not None:
        rejected.append(RejectedFact(fact.page_id, reason))
        return None
    return round_kg(fact.weight_value * KG_PER_UNIT[unit])


def round_kg(weight_kg: float) -> float:
    """Round to whole grams, half up (spec 11.4); internal weights have the same precision."""
    # str() first so 0.0025 is rounded as written, not as its binary approximation.
    grams = Decimal(str(weight_kg)).quantize(_GRAM, rounding=ROUND_HALF_UP)
    return float(grams)


def _weight_on_page(value: float, unit: str, text: str) -> bool:
    return any(
        abs(float(number.replace(",", "")) - value) < _FLOAT_SLACK and written.lower() == unit
        for number, written in WEIGHT_PATTERN.findall(text)
    )


def _evidence(page_id: str, snippet: str, source: str = EXTERNAL_SPEC_SOURCE) -> Evidence:
    return Evidence(source=source, ref=page_id, snippet=snippet[:200])
