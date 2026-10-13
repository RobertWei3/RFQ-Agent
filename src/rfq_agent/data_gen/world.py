"""Seeded "truth world": parts, numbers, chains, specs, prices and difficulty tags.

Everything here is fictional. Difficulty tags are assigned by exact quota
(``round(ratio * n)``) so realized ratios match the targets.
"""

from __future__ import annotations

import random
import statistics
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Literal

from rfq_agent.config import CostModel
from rfq_agent.costing.formula import compute_breakdown
from rfq_agent.data_gen.params import Difficulty, GenParams
from rfq_agent.data_gen.truth import TruthListing
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import CostBreakdown

D = Difficulty
CATEGORIES = ("thermostat", "water_pump")
RETAIL_SITES = tuple(f"retail_{i}" for i in range(1, 6))

MATERIAL_PRICES: dict[str, float] = {
    "aluminum": 3.20,
    "cast_iron": 1.20,
    "brass": 8.00,
    "stainless_steel": 4.00,
}
CATEGORY_MATERIALS: dict[str, tuple[str, ...]] = {
    "thermostat": ("aluminum", "brass", "stainless_steel"),
    "water_pump": ("cast_iron", "aluminum"),
}
WEIGHT_RANGE_KG = {"thermostat": (0.15, 0.60), "water_pump": (1.20, 5.00)}
CYCLE_RANGE_MIN = {"thermostat": (3.5, 7.0), "water_pump": (18.0, 32.0)}

_DIG = string.digits
_ALPHA = "ABCDEFGHJKLMNPRSTUVWXYZ"


@dataclass(frozen=True)
class Brand:
    name: str
    models: tuple[str, ...]
    make_number: Callable[[random.Random], str]


def _kestrel(r: random.Random) -> str:
    return f"{r.randint(10000, 99999)}-{r.choice(_DIG)}{r.choice(_ALPHA)}{r.choice(_DIG)}"


def _valtora(r: random.Random) -> str:
    head = f"{r.choice(_DIG)}{r.choice(_ALPHA)}{r.choice(_DIG)}"
    return f"{head}-{r.randint(100, 999)}-{r.randint(100, 999)}{r.choice(_ALPHA)}"


def _nordhaven(r: random.Random) -> str:
    return str(r.randint(10_000_000, 99_999_999))


def _sorrento(r: random.Random) -> str:
    head = f"{r.choice(_ALPHA)}{r.choice(_ALPHA)}{r.randint(10, 99)}"
    return f"{head}-{r.randint(1000, 9999)}-{r.choice(_ALPHA)}{r.choice(_ALPHA)}"


def _halvard(r: random.Random) -> str:
    return f"{r.randint(1000, 9999)}.{r.randint(10, 99)}.{r.randint(100, 999)}"


BRANDS: tuple[Brand, ...] = (
    Brand("Kestrel Motors", ("Aurora", "Strider", "Canyon", "Vela"), _kestrel),
    Brand("Valtora", ("Marino", "Sesto", "Corsa", "Lido"), _valtora),
    Brand("Nordhaven", ("Fjell", "Tundra", "Skerry", "Polar"), _nordhaven),
    Brand("Sorrento Auto", ("Amalfi", "Capri", "Positano", "Ravello"), _sorrento),
    Brand("Halvard", ("Ridge", "Summit", "Harbor", "Prairie"), _halvard),
)


@dataclass
class OEPart:
    oe_no: str
    brand: Brand
    category: str
    fitment: list[str]
    chain: list[str]  # oldest -> current; [oe_no] when there is no supersession


@dataclass
class Spec:
    category: str
    material: str
    weight_kg: float
    cycle_time_min: float


@dataclass
class OurPart:
    our_part_no: str
    description: str
    spec: Spec
    internal_material: str | None
    internal_weight_kg: float | None


@dataclass
class PriceBook:
    """Prices for one primary OE entity."""

    msrp: float
    msrp_status: Literal["found", "not_found", "discontinued"]
    msrp_source_oe: str | None
    listings: dict[str, TruthListing | None]
    outlier_sites: list[str]
    expected_selected_site: str | None
    secondary_msrp: dict[str, float] = field(default_factory=dict)


@dataclass
class Entity:
    """One physical product: shared by all rows of a many-to-one group."""

    entity_id: int
    category: str
    our_part: OurPart
    primary: OEPart | None
    others: list[OEPart]
    cost: CostBreakdown
    prices: PriceBook | None = None
    external_spec: Spec | None = None  # as published externally (may be corrupted)
    interchange_decoy: str | None = None


@dataclass
class Row:
    row_id: int
    entity: Entity
    customer_part_no: str
    customer_part_no_raw: str
    reachability: Literal["given", "xref", "unreachable"]
    tags: set[Difficulty]
    description: str | None
    annual_qty: int | None
    group: list[int] = field(default_factory=list)  # other row_ids sharing the OE


@dataclass
class World:
    params: GenParams
    rows: list[Row]
    entities: list[Entity]
    distractors: list[OurPart]
    material_prices: dict[str, float]


class _NumberRegistry:
    """Hands out part numbers that stay unique after normalization."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim(self, make: Callable[[], str]) -> str:
        for _ in range(1000):
            number = make()
            key = normalize_part_no(number)
            if key not in self._seen:
                self._seen.add(key)
                return number
        raise RuntimeError("Could not generate a unique part number")


def build_world(params: GenParams, cost_model: CostModel) -> World:
    rng = random.Random(params.seed)
    registry = _NumberRegistry()
    n = params.n_parts
    tags = _assign_tags(params, rng)
    groups = _many_to_one_groups(tags, rng)
    reach = _assign_reachability(params, rng)
    _assign_spec_tags(params, rng, tags, reach)

    entity_of_row: dict[int, Entity] = {}
    entities: list[Entity] = []
    for idx in range(n):
        if idx in entity_of_row:
            continue
        members = next((g for g in groups if idx in g), [idx])
        entity = _make_entity(len(entities), tags[idx], params, cost_model, rng, registry)
        entities.append(entity)
        for member in members:
            entity_of_row[member] = entity

    rows: list[Row] = []
    for idx in range(n):
        entity = entity_of_row[idx]
        prefix = "TS" if entity.category == "thermostat" else "WP"
        cust = registry.claim(partial(_customer_number, prefix, rng))
        raw = _dirty(cust, rng) if D.DIRTY_INPUT in tags[idx] else cust
        rows.append(
            Row(
                row_id=idx + 1,
                entity=entity,
                customer_part_no=cust,
                customer_part_no_raw=raw,
                reachability=reach[idx],
                tags=tags[idx],
                description=entity.our_part.description
                if rng.random() < params.description_share
                else None,
                annual_qty=rng.choice([50, 100, 200, 250, 500, 1000, 2000, 5000]),
            )
        )
    for group in groups:
        for idx in group:
            rows[idx].group = [m + 1 for m in group if m != idx]

    distractors = [
        _make_our_part(rng.choice(CATEGORIES), rng, registry)
        for _ in range(round(n * params.distractor_part_ratio))
    ]
    return World(params, rows, entities, distractors, dict(MATERIAL_PRICES))


# --- tag assignment -------------------------------------------------------


def _sample(pool: Sequence[int], k: int, rng: random.Random, what: Difficulty) -> set[int]:
    if k > len(pool):
        raise ValueError(f"Not enough eligible rows for {what}: need {k}, have {len(pool)}")
    return set(rng.sample(sorted(pool), k))


def _assign_tags(params: GenParams, rng: random.Random) -> list[set[Difficulty]]:
    n = params.n_parts
    tags: list[set[Difficulty]] = [set() for _ in range(n)]

    def mark(rows: set[int], tag: Difficulty) -> None:
        for i in rows:
            tags[i].add(tag)

    everything = list(range(n))
    not_found = _sample(everything, params.quota(D.OE_NOT_FOUND), rng, D.OE_NOT_FOUND)
    mark(not_found, D.OE_NOT_FOUND)
    found = [i for i in everything if i not in not_found]

    m2o_k = params.quota(D.MANY_TO_ONE)
    if m2o_k == 1:
        raise ValueError("many_to_one quota must be 0 or at least 2 rows")
    mark(_sample(found, m2o_k, rng, D.MANY_TO_ONE), D.MANY_TO_ONE)
    # Rows whose OE entity is not shared; entity-level difficulties are drawn from here.
    simple = [i for i in found if D.MANY_TO_ONE not in tags[i]]

    for tag in (
        D.SUPERSESSION,
        D.ONE_TO_MANY,
        D.RETAIL_MISSING,
        D.RETAIL_OUTLIER,
        D.RETAIL_VARIANT_SPELLING,
        D.MSRP_DISCONTINUED,
    ):
        mark(_sample(simple, params.quota(tag), rng, tag), tag)

    no_msrp_pool = [i for i in simple if D.MSRP_DISCONTINUED not in tags[i]]
    mark(_sample(no_msrp_pool, params.quota(D.MSRP_MISSING), rng, D.MSRP_MISSING), D.MSRP_MISSING)

    chains = [
        i
        for i in simple
        if D.SUPERSESSION in tags[i] and not tags[i] & {D.MSRP_DISCONTINUED, D.MSRP_MISSING}
    ]
    n_super = sum(D.SUPERSESSION in t for t in tags)
    fallback_k = min(round(n_super * params.msrp_fallback_share), len(chains))
    mark(_sample(chains, fallback_k, rng, D.MSRP_FALLBACK), D.MSRP_FALLBACK)

    mark(_sample(everything, params.quota(D.DIRTY_INPUT), rng, D.DIRTY_INPUT), D.DIRTY_INPUT)
    return tags


def _assign_spec_tags(
    params: GenParams,
    rng: random.Random,
    tags: list[set[Difficulty]],
    reach: list[Literal["given", "xref", "unreachable"]],
) -> None:
    eligible = [
        i
        for i, t in enumerate(tags)
        if reach[i] != "unreachable" and not t & {D.OE_NOT_FOUND, D.MANY_TO_ONE}
    ]
    missing = _sample(eligible, params.quota(D.INTERNAL_SPEC_MISSING), rng, D.INTERNAL_SPEC_MISSING)
    for i in missing:
        tags[i].add(D.INTERNAL_SPEC_MISSING)
    rest = [i for i in eligible if i not in missing]
    for i in _sample(rest, params.quota(D.SPEC_CONFLICT), rng, D.SPEC_CONFLICT):
        tags[i].add(D.SPEC_CONFLICT)


def _many_to_one_groups(tags: list[set[Difficulty]], rng: random.Random) -> list[list[int]]:
    members = [i for i, t in enumerate(tags) if D.MANY_TO_ONE in t]
    rng.shuffle(members)
    groups = [members[i : i + 2] for i in range(0, len(members) - len(members) % 2, 2)]
    if len(members) % 2:
        groups[-1].append(members[-1])
    return groups


def _assign_reachability(
    params: GenParams, rng: random.Random
) -> list[Literal["given", "xref", "unreachable"]]:
    n = params.n_parts
    order = list(range(n))
    rng.shuffle(order)
    n_given = round(n * params.our_part_given_ratio)
    n_xref = round(n * params.our_part_xref_ratio)
    result: list[Literal["given", "xref", "unreachable"]] = ["unreachable"] * n
    for i in order[:n_given]:
        result[i] = "given"
    for i in order[n_given : n_given + n_xref]:
        result[i] = "xref"
    return result


# --- entities ---------------------------------------------------------------


def _make_entity(
    entity_id: int,
    tags: set[Difficulty],
    params: GenParams,
    cost_model: CostModel,
    rng: random.Random,
    registry: _NumberRegistry,
) -> Entity:
    category = rng.choice(CATEGORIES)
    our_part = _make_our_part(category, rng, registry)
    if D.INTERNAL_SPEC_MISSING in tags:
        drop = rng.choice(("material", "weight", "both"))
        if drop in ("material", "both"):
            our_part.internal_material = None
        if drop in ("weight", "both"):
            our_part.internal_weight_kg = None

    spec = our_part.spec
    cost = compute_breakdown(
        weight_kg=spec.weight_kg,
        material_price_per_kg=MATERIAL_PRICES[spec.material],
        category=category,
        cost_model=cost_model,
        cycle_time_min=spec.cycle_time_min,
    )
    entity = Entity(entity_id, category, our_part, None, [], cost)
    if D.OE_NOT_FOUND in tags:
        return entity

    brand = rng.choice(BRANDS)
    chain_len = rng.choice((2, 3)) if D.SUPERSESSION in tags else 1
    chain = [registry.claim(partial(brand.make_number, rng)) for _ in range(chain_len)]
    entity.primary = OEPart(chain[-1], brand, category, _fitment(brand, rng), chain)

    if D.ONE_TO_MANY in tags:
        other_brands = [b for b in BRANDS if b is not brand]
        for other in rng.sample(other_brands, rng.choice((1, 2))):
            number = registry.claim(partial(other.make_number, rng))
            entity.others.append(OEPart(number, other, category, _fitment(other, rng), [number]))

    entity.prices = _make_prices(entity, tags, params, rng)

    needs_external = bool(tags & {D.INTERNAL_SPEC_MISSING, D.SPEC_CONFLICT})
    if needs_external or rng.random() < params.external_spec_share:
        entity.external_spec = _external_spec(spec, D.SPEC_CONFLICT in tags, rng)

    if rng.random() < 0.2:
        decoy_brand = rng.choice(BRANDS)
        entity.interchange_decoy = registry.claim(partial(decoy_brand.make_number, rng))
    return entity


def _make_our_part(category: str, rng: random.Random, registry: _NumberRegistry) -> OurPart:
    prefix = "THM" if category == "thermostat" else "WPM"
    base = 10000 if category == "thermostat" else 20000
    number = registry.claim(lambda: f"{prefix}-{base + rng.randint(0, 9999):05d}")
    material = rng.choice(CATEGORY_MATERIALS[category])
    w_lo, w_hi = WEIGHT_RANGE_KG[category]
    c_lo, c_hi = CYCLE_RANGE_MIN[category]
    spec = Spec(
        category=category,
        material=material,
        weight_kg=round(rng.uniform(w_lo, w_hi), 3),
        cycle_time_min=round(rng.uniform(c_lo, c_hi), 1),
    )
    return OurPart(number, _description(category, rng), spec, spec.material, spec.weight_kg)


def _description(category: str, rng: random.Random) -> str:
    if category == "thermostat":
        temp = rng.choice((82, 87, 88, 90, 92, 95))
        extra = rng.choice(("with gasket", "with housing", "with seal", "insert only"))
        return f"Engine coolant thermostat {temp}C {extra}"
    extra = rng.choice(("with gasket", "with pulley", "with housing", "metal impeller"))
    return f"Engine water pump {extra}"


def _fitment(brand: Brand, rng: random.Random) -> list[str]:
    result = []
    for model in rng.sample(brand.models, rng.choice((1, 2))):
        engine = rng.choice(("1.4L", "1.6L", "1.8L", "2.0L", "2.4L", "3.0L V6"))
        start = rng.randint(2008, 2019)
        result.append(f"{brand.name} {model} {engine} {start}-{start + rng.randint(2, 6)}")
    return result


def _customer_number(prefix: str, rng: random.Random) -> str:
    number = f"{prefix}-{rng.randint(10000, 99999)}"
    return number + rng.choice(("", "", "", "-A", "-B", "-HD"))


def _dirty(number: str, rng: random.Random) -> str:
    """Raw spelling with full-width chars and/or stray spaces that normalizes back."""
    style = rng.choice(("fullwidth", "spaces", "both"))
    text = number
    if style in ("fullwidth", "both"):
        text = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)
    if style in ("spaces", "both"):
        text = f"  {text.replace('-', ' - ', 1)} "
    return text


def variant_spelling(number: str, rng: random.Random) -> str:
    """Different case/separators, same normalized key."""
    key = normalize_part_no(number)
    options = [
        number.lower(),
        number.replace("-", " ").replace(".", " "),
        key,
        key.lower(),
        f"{key[:4]}-{key[4:]}",
        f"{key[:3]} {key[3:]}",
    ]
    options = [o for o in options if o != number]
    return rng.choice(options)


# --- prices -----------------------------------------------------------------


def _make_prices(
    entity: Entity, tags: set[Difficulty], params: GenParams, rng: random.Random
) -> PriceBook:
    assert entity.primary is not None
    landed = entity.cost.landed_cost
    price_range = params.msrp_ranges[entity.category]
    # landed = ref_list * landed_share and ref_list = msrp * list_share; resample until the
    # MSRP falls in the category range, clamping as a last resort.
    msrp = list_share = 0.0
    for _ in range(50):
        list_share = rng.uniform(*params.retail_list_of_msrp)
        msrp = landed / rng.uniform(*params.landed_of_retail) / list_share
        if price_range.low <= msrp <= price_range.high:
            break
    msrp = round(min(max(msrp, price_range.low), price_range.high), 2)

    ref_list = msrp * list_share
    listings: dict[str, TruthListing | None] = {}
    for site in RETAIL_SITES:
        site_list = ref_list * rng.uniform(0.92, 1.08)
        lo, hi = params.retail_list_of_msrp
        list_price = round(min(max(site_list, msrp * lo), msrp * hi), 2)
        sale = None
        if rng.random() < params.discount_share:
            sale = round(list_price * (1 - rng.uniform(*params.discount_pct)), 2)
        listings[site] = TruthListing(
            title_part_no=entity.primary.oe_no,
            list_price=list_price,
            sale_price=sale,
            effective_price=sale if sale is not None else list_price,
            in_stock=rng.random() >= params.out_of_stock_share,
            match_quality="exact",
            outlier=False,
        )

    if D.RETAIL_MISSING in tags:
        n_missing = 1 if D.RETAIL_OUTLIER in tags else rng.choice((1, 2, 3))
        for site in rng.sample(RETAIL_SITES, n_missing):
            listings[site] = None

    present = [s for s in RETAIL_SITES if listings[s] is not None]
    outlier_sites: list[str] = []
    if D.RETAIL_OUTLIER in tags:
        site = rng.choice(present)
        others = [listings[s].effective_price for s in present if s != site]  # type: ignore[union-attr]
        factor = rng.uniform(0.3, 0.5) if rng.random() < 0.5 else rng.uniform(1.8, 3.0)
        price = round(statistics.median(others) * factor, 2)
        listings[site] = TruthListing(
            title_part_no=entity.primary.oe_no,
            list_price=price,
            sale_price=None,
            effective_price=price,
            in_stock=True,
            match_quality="exact",
            outlier=True,
        )
        outlier_sites.append(site)

    if D.RETAIL_VARIANT_SPELLING in tags:
        for site in rng.sample(present, min(len(present), rng.choice((2, 3)))):
            listing = listings[site]
            assert listing is not None
            listing.title_part_no = variant_spelling(entity.primary.oe_no, rng)
            listing.match_quality = "normalized"

    chain = entity.primary.chain
    if D.MSRP_MISSING in tags:
        status: Literal["found", "not_found", "discontinued"] = "not_found"
        source: str | None = None
    elif D.MSRP_FALLBACK in tags:
        status, source = "found", chain[-2]
    else:
        status = "discontinued" if D.MSRP_DISCONTINUED in tags else "found"
        source = chain[-1]

    secondary = {o.oe_no: round(msrp * rng.uniform(0.9, 1.15), 2) for o in entity.others}
    return PriceBook(
        msrp=msrp,
        msrp_status=status,
        msrp_source_oe=source,
        listings=listings,
        outlier_sites=outlier_sites,
        expected_selected_site=expected_selected_site(listings),
        secondary_msrp=secondary,
    )


_MATCH_TIER = {"exact": 0, "normalized": 1, "fuzzy": 2}


def expected_selected_site(listings: dict[str, TruthListing | None]) -> str | None:
    """Section 11.3 selection rule (tiered as confirmed in P4).

    Among non-outlier listings: keep the best match tier present (exact >
    normalized > fuzzy); then keep in-stock ones if any; pick the lowest
    effective price (ties broken by site name). Implemented independently of
    the agent's ``select_retail`` so evaluation does not check code against itself.
    """
    pool = [(s, lst) for s, lst in listings.items() if lst is not None and not lst.outlier]
    if not pool:
        return None
    best = min(_MATCH_TIER[p[1].match_quality] for p in pool)
    pool = [p for p in pool if _MATCH_TIER[p[1].match_quality] == best]
    in_stock = [p for p in pool if p[1].in_stock]
    pool = in_stock or pool
    return min(pool, key=lambda p: (p[1].effective_price, p[0]))[0]


def _external_spec(spec: Spec, corrupt: bool, rng: random.Random) -> Spec:
    if not corrupt:
        return Spec(spec.category, spec.material, spec.weight_kg, spec.cycle_time_min)
    alternatives = [m for m in CATEGORY_MATERIALS[spec.category] if m != spec.material]
    if rng.random() < 0.5:
        factor = rng.uniform(1.25, 1.40) if rng.random() < 0.5 else rng.uniform(0.60, 0.75)
        return Spec(spec.category, spec.material, round(spec.weight_kg * factor, 3), 0)
    return Spec(spec.category, rng.choice(alternatives), spec.weight_kg, 0)
