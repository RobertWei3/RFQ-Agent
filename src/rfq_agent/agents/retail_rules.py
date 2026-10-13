"""Deterministic retail pricing rules (spec section 11.3).

Pure functions so the business rules can be tested and adjusted without the
subgraph, tools or an LLM.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

from rfq_agent.agents.common import non_usd, page_mentions, price_in_text
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import Evidence, MatchQuality, PriceQuote

MIN_QUOTES_FOR_OUTLIERS = 3  # with two quotes both deviate equally from their median


@dataclass
class OutlierCheck:
    outliers: list[str]  # quote sources judged to be outliers
    median: float | None  # None when there were too few quotes to check


def detect_outliers(quotes: Sequence[PriceQuote], threshold: float) -> OutlierCheck:
    """Flag quotes whose effective price deviates from the median by more than ``threshold``."""
    prices = [q.effective_price for q in quotes if q.effective_price is not None]
    if len(prices) < MIN_QUOTES_FOR_OUTLIERS:
        return OutlierCheck(outliers=[], median=None)
    median = statistics.median(prices)
    if median <= 0:  # prices are validated > 0 upstream; guard the division anyway
        return OutlierCheck(outliers=[], median=None)
    outliers = [
        q.source
        for q in quotes
        if q.effective_price is not None and abs(q.effective_price - median) / median > threshold
    ]
    return OutlierCheck(outliers=outliers, median=median)


MATCH_RANK: dict[MatchQuality, int] = {"exact": 0, "normalized": 1, "fuzzy": 2}


def select_retail(quotes: Sequence[PriceQuote], outliers: Sequence[str]) -> PriceQuote | None:
    """Pick the retail quote used as the pricing reference.

    Drop outliers, keep the best match tier present, prefer quotes confirmed in stock
    (unknown stock counts as out), then take the lowest effective price (ties: source).
    """
    pool = [q for q in quotes if q.source not in outliers and q.effective_price is not None]
    if not pool:
        return None
    best_tier = min(MATCH_RANK[q.match_quality] for q in pool)
    pool = [q for q in pool if MATCH_RANK[q.match_quality] == best_tier]
    pool = [q for q in pool if q.in_stock is True] or pool
    return min(pool, key=lambda q: (q.effective_price or 0.0, q.source))


# --- page facts -> one quote per site ---------------------------------------------------


class RetailListing(BaseModel):
    """Facts the LLM extracts from one retail search result page."""

    page_id: str
    listed_part_no: str | None = Field(
        description="Part number in the listing title, exactly as written."
    )
    list_price: float | None = Field(
        default=None, description="Regular / list / 'was' price, as a plain number."
    )
    sale_price: float | None = Field(
        default=None, description="Discounted / sale / 'now' price, as a plain number."
    )
    currency: str | None = Field(default=None, description="ISO currency code, e.g. USD.")
    in_stock: bool | None = Field(
        default=None, description="True / false if the page states availability, else null."
    )
    price_snippet: str | None = Field(default=None, description="The line stating the price.")


@dataclass(frozen=True)
class RetailPage:
    site: str  # quote source, e.g. "retail_3"
    text: str


@dataclass
class RejectedListing:
    """An extracted listing that deterministic checks rejected (kept for the trace)."""

    page_id: str
    reason: str


@dataclass
class QuoteBuild:
    quotes: list[PriceQuote]  # at most one per site, sorted by site
    rejected: list[RejectedListing] = field(default_factory=list)


class _Rejected(Exception):
    """Internal: a listing cannot be used; the message is the reason."""


def build_quotes(
    chain: Sequence[str], listings: Sequence[RetailListing], pages: dict[str, RetailPage]
) -> QuoteBuild:
    """Turn extracted page facts into at most one grounded USD quote per site."""
    built = QuoteBuild(quotes=[])
    by_site: dict[str, list[PriceQuote]] = {}
    for listing in listings:
        try:
            site, quote = _accept(chain, listing, pages)
        except _Rejected as rejected:
            built.rejected.append(RejectedListing(listing.page_id, str(rejected)))
            continue
        by_site.setdefault(site, []).append(quote)
    for site in sorted(by_site):
        built.quotes.append(_one_per_site(site, by_site[site], built.rejected))
    return built


def _accept(
    chain: Sequence[str], listing: RetailListing, pages: dict[str, RetailPage]
) -> tuple[str, PriceQuote]:
    """(site, quote) for a usable listing; raises ``_Rejected`` with the reason otherwise."""
    page = pages.get(listing.page_id)
    if page is None:
        raise _Rejected("unknown page_id")
    quality, note = _match(chain, listing.listed_part_no)
    _check_grounded(listing, page.text)
    return page.site, _quote(page.site, listing, quality, note)


def _one_per_site(
    site: str, quotes: list[PriceQuote], rejected: list[RejectedListing]
) -> PriceQuote:
    """Best match quality, then confirmed in stock, then lowest price, then page_id."""
    ranked = sorted(
        quotes,
        key=lambda q: (
            MATCH_RANK[q.match_quality],
            q.in_stock is not True,
            q.effective_price or 0.0,
            q.evidence[0].ref,
        ),
    )
    best = ranked[0]
    if len(ranked) == 1:
        return best
    chosen = best.evidence[0].ref
    for loser in ranked[1:]:
        rejected.append(
            RejectedListing(loser.evidence[0].ref, f"another page on {site} was chosen ({chosen})")
        )
    note = f"{len(ranked)} matching pages on {site}; chose {chosen}"
    reasoning = f"{best.reasoning}; {note}" if best.reasoning else note
    return best.model_copy(update={"reasoning": reasoning})


def _check_grounded(listing: RetailListing, text: str) -> None:
    """Raise ``_Rejected`` unless the listing's part number and prices are on the page."""
    written = (listing.listed_part_no or "").strip()
    prices = [p for p in (listing.list_price, listing.sale_price) if p is not None]
    if not page_mentions(text, normalize_part_no(written)):
        raise _Rejected(f"listed part number {written} not found in page text")
    if not prices:
        raise _Rejected("page shows no price")
    for price in prices:
        if price <= 0:
            raise _Rejected(f"non-positive price {price}")
        if not price_in_text(price, text):
            raise _Rejected(f"price {price} not found in page text")
    code = non_usd(listing.currency)
    if code is not None:
        raise _Rejected(f"non-USD price ({code}); only USD is used")


def _match(chain: Sequence[str], listed: str | None) -> tuple[MatchQuality, str]:
    """(match quality, note) for a listed part number; raises ``_Rejected`` if off-chain."""
    written = (listed or "").strip()
    key = normalize_part_no(written)
    current = chain[-1]
    if written == current:
        return "exact", ""
    if key == normalize_part_no(current):
        return "normalized", f"listed as {written!r} (same number as {current} after normalization)"
    for older in chain[:-1]:
        if key == normalize_part_no(older):
            return "fuzzy", f"listed under superseded number {older}"
    raise _Rejected(f"listed part number {written or '(none)'} is not on the supersession chain")


def _quote(site: str, listing: RetailListing, quality: MatchQuality, note: str) -> PriceQuote:
    list_price, sale_price = listing.list_price, listing.sale_price
    notes = [note] if note else []
    if list_price is not None and sale_price is not None and sale_price >= list_price:
        notes.append(f"sale price {sale_price} is not below list price; ignored")
        sale_price = None
    effective = sale_price if sale_price is not None else list_price
    discount = (
        discount_pct(list_price, sale_price) if list_price and sale_price is not None else None
    )
    if listing.in_stock is None:
        notes.append("page does not state stock status (treated as not in stock)")
    return PriceQuote(
        source=site,
        price_type="retail",
        matched_part_no=(listing.listed_part_no or "").strip(),
        list_price=list_price,
        sale_price=sale_price,
        discount_pct=discount,
        effective_price=effective,
        currency="USD",
        in_stock=listing.in_stock,
        match_quality=quality,
        reasoning="; ".join(notes),
        evidence=[Evidence(source=site, ref=listing.page_id, snippet=listing.price_snippet)],
    )


def discount_pct(list_price: float, sale_price: float) -> float:
    """Discount in percent, rounded half-up to one decimal (decimal arithmetic, no float drift)."""
    ratio = (Decimal(str(list_price)) - Decimal(str(sale_price))) / Decimal(str(list_price))
    return float((ratio * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
