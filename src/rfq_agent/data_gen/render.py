"""Render the truth world into noisy text pages, one corpus per mock tool."""

from __future__ import annotations

import random
from dataclasses import dataclass

from rfq_agent.data_gen.world import RETAIL_SITES, Entity, OEPart, Spec, World
from rfq_agent.partno import normalize_part_no

RETAIL_SITE_NAMES = {
    "retail_1": "PartsHarbor",
    "retail_2": "AutoDepot Direct",
    "retail_3": "GearLane",
    "retail_4": "MotorMart Online",
    "retail_5": "RapidAuto Supply",
}

MATERIAL_PHRASES = {
    "aluminum": ("aluminium", "die-cast aluminum", "Al alloy (ADC12)", "aluminum"),
    "cast_iron": ("grey cast iron", "cast iron", "GG25 iron"),
    "brass": ("brass", "CuZn brass", "yellow brass"),
    "stainless_steel": ("stainless steel", "SS304", "stainless steel (304)"),
}

CATEGORY_WORDS = {
    "thermostat": ("Thermostat", "Coolant Thermostat", "Engine Thermostat Assy"),
    "water_pump": ("Water Pump", "Engine Water Pump", "Coolant Pump"),
}

_BOILERPLATE = (
    "Free shipping on orders over $75.",
    "Prices subject to change without notice.",
    "Images for illustration only.",
    "Check fitment with your VIN before ordering.",
    "Sign up for our newsletter for 10% off.",
    "Warranty: 12 months / 12,000 miles.",
    "Dealer network pricing may vary by region.",
)


@dataclass(frozen=True)
class Page:
    page_id: str
    title: str
    text: str
    keys: tuple[str, ...]  # normalized part numbers this page is indexed under


def render_corpora(world: World) -> dict[str, list[Page]]:
    rng = random.Random(f"{world.params.seed}-render")
    corpora: dict[str, list[Page]] = {
        "oem_catalog": [],
        "interchange": [],
        "msrp": [],
        "external_spec": [],
        **{site: [] for site in RETAIL_SITES},
    }
    for entity in world.entities:
        if entity.primary is None:
            continue
        for part in [entity.primary, *entity.others]:
            corpora["oem_catalog"].extend(_catalog_pages(part, rng))
        corpora["msrp"].extend(_msrp_pages(entity, rng))
        for site in RETAIL_SITES:
            page = _retail_page(entity, site, rng)
            if page is not None:
                corpora[site].append(page)
        if entity.external_spec is not None:
            corpora["external_spec"].append(_spec_page(entity.primary, entity.external_spec, rng))

    for row in world.rows:
        page = _interchange_page(row.customer_part_no, row.entity, rng)
        if page is not None:
            corpora["interchange"].append(page)

    for name, pages in corpora.items():
        corpora[name] = sorted(pages, key=lambda p: p.page_id)
        _check_unique_ids(name, corpora[name])
    return corpora


def _check_unique_ids(name: str, pages: list[Page]) -> None:
    ids = [p.page_id for p in pages]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"Duplicate page ids in corpus {name}")


def _noise(rng: random.Random, k: int = 2) -> str:
    return " ".join(rng.sample(_BOILERPLATE, k))


def _pid(prefix: str, number: str) -> str:
    return f"{prefix}-{normalize_part_no(number)}"


def _catalog_pages(part: OEPart, rng: random.Random) -> list[Page]:
    pages = []
    chain = part.chain
    for i, number in enumerate(chain):
        lines = [
            f"{part.brand.name} Genuine Parts Catalog - section {rng.randint(10, 99)}",
            rng.choice(
                (f"Part Number: {number}", f"P/N {number}", f"OE# {number}", f"Part No.: {number}")
            ),
            f"Description: {rng.choice(CATEGORY_WORDS[part.category])}",
            "Fits: " + "; ".join(part.fitment),
        ]
        if i + 1 < len(chain):
            successor = chain[i + 1]
            lines.append(
                rng.choice(
                    (
                        f"Superseded by: {successor}",
                        f"SUPERSEDED BY {successor} - order new number",
                        f"This part has been replaced. Superseded by {successor}.",
                    )
                )
            )
        if i > 0:
            predecessor = chain[i - 1]
            lines.append(
                rng.choice(
                    (
                        f"Replaces: {predecessor}",
                        f"REPLACES {predecessor}",
                        f"Replaces old number {predecessor}.",
                    )
                )
            )
        lines.append(_noise(rng))
        pages.append(
            Page(
                page_id=_pid("cat", number),
                title=f"{part.brand.name} catalog {number}",
                text="\n".join(lines),
                keys=(normalize_part_no(number),),
            )
        )
    return pages


def _msrp_pages(entity: Entity, rng: random.Random) -> list[Page]:
    prices = entity.prices
    primary = entity.primary
    assert prices is not None and primary is not None
    pages = []
    if prices.msrp_source_oe is not None:
        pages.append(
            _msrp_page(
                primary,
                prices.msrp_source_oe,
                prices.msrp,
                prices.msrp_status == "discontinued",
                rng,
            )
        )
    # Older chain numbers that are not the price source: a redirect without a price.
    for number in primary.chain[:-1]:
        if number != prices.msrp_source_oe and rng.random() < 0.5:
            successor = primary.chain[primary.chain.index(number) + 1]
            text = (
                f"{primary.brand.name} Parts Online - price lookup\n"
                f"Part {number} is no longer sold under this number. "
                f"Please order {successor}.\n{_noise(rng, 1)}"
            )
            pages.append(
                Page(_pid("msrp", number), f"{number} price", text, (normalize_part_no(number),))
            )
    for other in entity.others:
        pages.append(_msrp_page(other, other.oe_no, prices.secondary_msrp[other.oe_no], False, rng))
    return pages


def _msrp_page(
    part: OEPart, number: str, price: float, discontinued: bool, rng: random.Random
) -> Page:
    price_line = rng.choice(
        (
            f"MSRP: ${price:.2f} USD",
            f"Manufacturer's suggested retail price ${price:,.2f}",
            f"List price (MSRP) USD {price:.2f}",
        )
    )
    lines = [f"{part.brand.name} Parts Online - price lookup", f"Part: {number}", price_line]
    if discontinued:
        lines.append(rng.choice(("Status: DISCONTINUED", "This item is discontinued.")))
    lines.append(_noise(rng, 1))
    return Page(
        _pid("msrp", number), f"{number} price", "\n".join(lines), (normalize_part_no(number),)
    )


def _retail_page(entity: Entity, site: str, rng: random.Random) -> Page | None:
    prices, primary = entity.prices, entity.primary
    assert prices is not None and primary is not None
    listing = prices.listings[site]
    if listing is None:
        return None
    name = RETAIL_SITE_NAMES[site]
    word = rng.choice(CATEGORY_WORDS[entity.category])
    fit = primary.fitment[0]
    title = f"{primary.brand.name} {word} {listing.title_part_no} for {fit}"
    if listing.sale_price is not None:
        price_line = rng.choice(
            (
                f"Was ${listing.list_price:.2f} Now ${listing.sale_price:.2f}",
                f"List: ${listing.list_price:.2f} | Sale: ${listing.sale_price:.2f}",
                f"Reg. {listing.list_price:.2f} USD - our price {listing.sale_price:.2f} USD",
            )
        )
    else:
        price_line = rng.choice(
            (f"Price: ${listing.list_price:.2f}", f"${listing.list_price:.2f} each")
        )
    stock = (
        rng.choice(("In stock", "Ships today", "Available"))
        if listing.in_stock
        else rng.choice(("Out of stock", "Backordered", "Currently unavailable"))
    )
    text = f"{name} | {title}\n{price_line}\n{stock}\n{_noise(rng)}"
    return Page(
        page_id=f"{site}-{normalize_part_no(primary.oe_no)}",
        title=title,
        text=text,
        keys=(normalize_part_no(listing.title_part_no),),
    )


def _spec_page(part: OEPart, spec: Spec, rng: random.Random) -> Page:
    material = rng.choice(MATERIAL_PHRASES[spec.material])
    unit = rng.random()
    if unit < 0.5:
        weight = f"{spec.weight_kg:.3f} kg"
    elif unit < 0.8:
        weight = f"{spec.weight_kg * 1000:.0f} g"
    else:
        weight = f"{spec.weight_kg * 2.20462:.3f} lb"
    lines = [
        f"Product data sheet - {part.brand.name} {part.oe_no}",
        rng.choice((f"Housing material: {material}", f"Material: {material}", f"Body: {material}")),
        rng.choice(
            (f"Net weight: {weight}", f"Weight {weight}", f"Shipping weight approx. {weight}")
        ),
        _noise(rng, 1),
    ]
    return Page(
        page_id=_pid("spec", part.oe_no),
        title=f"{part.oe_no} data sheet",
        text="\n".join(lines),
        keys=(normalize_part_no(part.oe_no),),
    )


def _interchange_page(customer_no: str, entity: Entity, rng: random.Random) -> Page | None:
    key = normalize_part_no(customer_no)
    primary = entity.primary
    if primary is None:
        if rng.random() < 0.5:
            return None
        text = f"Interchange lookup for {customer_no}\nNo OE interchange found.\n{_noise(rng, 1)}"
        return Page(f"ic-{key}", f"Interchange {customer_no}", text, (key,))

    lines = [f"Interchange lookup for {customer_no}", "Candidate OE numbers:"]
    # Supersession rows often only know an older number; the catalog resolves the chain.
    shown = primary.chain[0] if len(primary.chain) > 1 and rng.random() < 0.6 else primary.oe_no
    lines.append(f"- {primary.brand.name} {shown} ({primary.fitment[0]})")
    for other in entity.others:
        lines.append(f"- {other.brand.name} {other.oe_no} ({other.fitment[0]})")
    if entity.interchange_decoy is not None:
        lines.append(f"Similar but NOT interchangeable: {entity.interchange_decoy}")
    lines.append(_noise(rng, 1))
    return Page(f"ic-{key}", f"Interchange {customer_no}", "\n".join(lines), (key,))
