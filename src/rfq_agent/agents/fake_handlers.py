"""Rule-based FakeLLM handlers for agent extraction schemas.

They parse only the prompt text (the rendered search results) with regular
expressions. They never read ground truth and are not meant to be accurate on
arbitrary pages, only to produce valid structured output deterministically.
"""

from __future__ import annotations

import re
from typing import Any

from rfq_agent.agents.should_cost_rules import KG_PER_UNIT, WEIGHT_PATTERN
from rfq_agent.llm.fake import FakeRequest, register_fake_handler
from rfq_agent.partno import normalize_part_no

_PN = r"([0-9A-Za-z][0-9A-Za-z.\-]*[0-9A-Za-z])"
_PAGE = re.compile(r"^=== page_id: (\S+)\n(.*?)(?=^=== page_id: |\Z)", re.M | re.S)
_CUSTOMER = re.compile(r"Customer part number: (.+)")
_LOOKUP_FOR = re.compile(r"Interchange lookup for (.+)")
_CANDIDATE = re.compile(rf"^- (.+?) {_PN} \(", re.M)
_SUBJECT = re.compile(rf"(?:Part Number:|P/N|OE#|Part No\.:)\s*{_PN}")
_SUPERSEDED = re.compile(rf"superseded by:?\s+{_PN}", re.I)
_REPLACES = re.compile(rf"replaces(?: old number)?:?\s+{_PN}", re.I)
_BRAND = re.compile(r"^(.+?) Genuine Parts Catalog", re.M)


def _pages(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _PAGE.finditer(text)]


@register_fake_handler("InterchangeExtraction")
def fake_interchange(request: FakeRequest) -> dict[str, Any]:
    text = request.text
    customer = _CUSTOMER.search(text)
    wanted = normalize_part_no(customer.group(1)) if customer else ""
    candidates = []
    for page_id, body in _pages(text):
        subject = _LOOKUP_FOR.search(body)
        if not subject or normalize_part_no(subject.group(1)) != wanted:
            continue
        for match in _CANDIDATE.finditer(body):
            candidates.append(
                {
                    "oe_part_no": match.group(2),
                    "brand": match.group(1),
                    "page_id": page_id,
                    "snippet": match.group(0),
                }
            )
    return {"candidates": candidates, "reasoning": f"[fake] {len(candidates)} candidate(s) listed."}


@register_fake_handler("CatalogExtraction")
def fake_catalog(request: FakeRequest) -> dict[str, Any]:
    pages = []
    for page_id, body in _pages(request.text):
        subject = _SUBJECT.search(body)
        newer = _SUPERSEDED.search(body)
        older = _REPLACES.search(body)
        brand = _BRAND.search(body)
        relation = newer or older
        snippet = relation.group(0) if relation else None
        pages.append(
            {
                "page_id": page_id,
                "subject_part_no": subject.group(1) if subject else None,
                "brand": brand.group(1) if brand else None,
                "superseded_by": newer.group(1) if newer else None,
                "replaces": older.group(1) if older else None,
                "snippet": snippet,
            }
        )
    return {"pages": pages, "reasoning": f"[fake] parsed {len(pages)} page(s)."}


_MSRP_SUBJECT = re.compile(rf"^Part: {_PN}|^Part {_PN} is no longer sold", re.M)
_MSRP_DISCONTINUED = re.compile(r"^.*discontinued.*$", re.I | re.M)
_MSRP_PRICE = re.compile(r"(?:MSRP: \$|retail price \$|\(MSRP\) USD )([\d,]+\.\d{2})")


@register_fake_handler("MSRPPageExtraction")
def fake_msrp(request: FakeRequest) -> dict[str, Any]:
    pages = []
    for page_id, body in _pages(request.text):
        subject = _MSRP_SUBJECT.search(body)
        price = _MSRP_PRICE.search(body)
        pages.append(
            {
                "page_id": page_id,
                "subject_part_no": (subject.group(1) or subject.group(2)) if subject else None,
                "price": float(price.group(1).replace(",", "")) if price else None,
                "currency": "USD" if price else None,
                "discontinued": "discontinued" in body.lower(),
                "discontinued_snippet": (
                    m.group(0).strip() if (m := _MSRP_DISCONTINUED.search(body)) else None
                ),
                "snippet": price.group(0) if price else None,
            }
        )
    return {"pages": pages, "reasoning": f"[fake] parsed {len(pages)} price page(s)."}


_RETAIL_TITLE = re.compile(r"^title: .*(?:Thermostat|Pump|Assy) (.+?) for ", re.M)
_RETAIL_SALE = re.compile(
    r"Was \$([\d,]+\.\d{2}) Now \$([\d,]+\.\d{2})"
    r"|List: \$([\d,]+\.\d{2}) \| Sale: \$([\d,]+\.\d{2})"
    r"|Reg\. ([\d,]+\.\d{2}) USD - our price ([\d,]+\.\d{2}) USD"
)
_RETAIL_PRICE = re.compile(r"^(?:Price: \$([\d,]+\.\d{2})|\$([\d,]+\.\d{2}) each)$", re.M)
_OUT_OF_STOCK = ("out of stock", "backordered", "currently unavailable")
_IN_STOCK = ("in stock", "ships today", "available")


def _money(value: str | None) -> float | None:
    return float(value.replace(",", "")) if value else None


def _stock(body: str) -> bool | None:
    lower = body.lower()
    if any(p in lower for p in _OUT_OF_STOCK):
        return False
    return True if any(p in lower for p in _IN_STOCK) else None


@register_fake_handler("RetailListingExtraction")
def fake_retail(request: FakeRequest) -> dict[str, Any]:
    listings = []
    for page_id, body in _pages(request.text):
        title = _RETAIL_TITLE.search(body)
        sale = _RETAIL_SALE.search(body)
        plain = _RETAIL_PRICE.search(body)
        list_price = sale_price = None
        snippet = None
        if sale:
            groups = [g for g in sale.groups() if g]
            list_price, sale_price = _money(groups[0]), _money(groups[1])
            snippet = sale.group(0)
        elif plain:
            list_price = _money(plain.group(1) or plain.group(2))
            snippet = plain.group(0)
        listings.append(
            {
                "page_id": page_id,
                "listed_part_no": title.group(1) if title else None,
                "list_price": list_price,
                "sale_price": sale_price,
                "currency": "USD" if snippet else None,
                "in_stock": _stock(body),
                "price_snippet": snippet,
            }
        )
    return {"listings": listings, "reasoning": f"[fake] parsed {len(listings)} listing page(s)."}


_SPEC_MATERIAL = re.compile(r"^(?:Housing material|Material|Body): (.+)$", re.M)
_CATALOG_DESCRIPTION = re.compile(r"^Description: (.+)$", re.M)
# Keyword guesses only; a real model reads the phrase against the known material keys.
_MATERIAL_WORDS = (
    ("stainless", "stainless_steel"),
    ("ss304", "stainless_steel"),
    ("iron", "cast_iron"),
    ("brass", "brass"),
    ("alumin", "aluminum"),
    ("al alloy", "aluminum"),
)


def _guess_material_key(phrase: str) -> str:
    lower = phrase.lower()
    return next((key for word, key in _MATERIAL_WORDS if word in lower), "other")


@register_fake_handler("SpecExtraction")
def fake_spec(request: FakeRequest) -> dict[str, Any]:
    pages = []
    for page_id, body in _pages(request.text):
        material = _SPEC_MATERIAL.search(body)
        weight = next(
            (m for m in WEIGHT_PATTERN.finditer(body) if m.group(2).lower() in KG_PER_UNIT), None
        )
        category = _CATALOG_DESCRIPTION.search(body)
        pages.append(
            {
                "page_id": page_id,
                "material_text": material.group(1).strip() if material else None,
                "material_key": _guess_material_key(material.group(1)) if material else None,
                "weight_value": float(weight.group(1).replace(",", "")) if weight else None,
                "weight_unit": weight.group(2) if weight else None,
                "category_text": category.group(1).strip() if category else None,
            }
        )
    return {"pages": pages, "reasoning": f"[fake] parsed {len(pages)} spec page(s)."}
