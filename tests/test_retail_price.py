"""retail_price tests: pure pricing rules, subgraph against generated pages, CLI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from rfq_agent.agents.common import RowFailure
from rfq_agent.agents.retail_price import RetailContext, resolve_retail
from rfq_agent.agents.retail_rules import (
    RetailListing,
    RetailPage,
    build_quotes,
    detect_outliers,
    select_retail,
)
from rfq_agent.cli import app
from rfq_agent.config import Settings
from rfq_agent.data_gen.audit import load_truth
from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.llm.fake import FakeRequest, register_fake_handler
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import OEResolution, PriceQuote, RetailResult
from rfq_agent.tools import build_tools
from rfq_agent.tools.base import ToolHit, ToolTimeoutError

# --- detect_outliers (pure) -----------------------------------------------------------


def _quote(source: str, price: float, **kw: Any) -> PriceQuote:
    fields: dict[str, Any] = {
        "source": source,
        "price_type": "retail",
        "matched_part_no": "NEW-3",
        "list_price": price,
        "sale_price": None,
        "discount_pct": None,
        "effective_price": price,
        "in_stock": True,
        "match_quality": "exact",
        "reasoning": "",
        "evidence": [],
    }
    fields.update(kw)
    return PriceQuote(**fields)


def test_price_far_above_median_is_outlier() -> None:
    # median of (50, 52, 48, 51, 120) is 51; 120 is +135%, the others are within 6%.
    quotes = [
        _quote("retail_1", 50.0),
        _quote("retail_2", 52.0),
        _quote("retail_3", 48.0),
        _quote("retail_4", 51.0),
        _quote("retail_5", 120.0),
    ]
    check = detect_outliers(quotes, threshold=0.40)
    assert check.outliers == ["retail_5"]
    assert check.median == 51.0


def test_fewer_than_three_quotes_skips_detection() -> None:
    # 50 vs 150 would both be 50% off their mean; with two quotes neither is flagged.
    check = detect_outliers([_quote("retail_1", 50.0), _quote("retail_2", 150.0)], 0.40)
    assert check.outliers == []
    assert check.median is None
    assert detect_outliers([], 0.40).outliers == []


def test_threshold_is_strict_and_low_prices_count() -> None:
    # median of (25, 50, 50, 50, 70) is 50: 70 is exactly +40% (kept), 25 is -50% (outlier).
    quotes = [
        _quote("retail_1", 25.0),
        _quote("retail_2", 50.0),
        _quote("retail_3", 50.0, in_stock=False),
        _quote("retail_4", 50.0, match_quality="fuzzy"),
        _quote("retail_5", 70.0),
    ]
    assert detect_outliers(quotes, 0.40).outliers == ["retail_1"]


def test_median_includes_out_of_stock_and_fuzzy_quotes() -> None:
    # Only the two in-stock exact quotes would give median 100 and flag nothing;
    # all five give median 40, so the 100s are outliers.
    quotes = [
        _quote("retail_1", 100.0),
        _quote("retail_2", 100.0),
        _quote("retail_3", 40.0, in_stock=False),
        _quote("retail_4", 40.0, match_quality="fuzzy"),
        _quote("retail_5", 38.0, in_stock=None),
    ]
    check = detect_outliers(quotes, 0.40)
    assert check.median == 40.0
    assert check.outliers == ["retail_1", "retail_2"]


# --- select_retail (pure) -------------------------------------------------------------


def test_exact_beats_cheaper_normalized_and_outliers_are_excluded() -> None:
    quotes = [
        _quote("retail_1", 10.0),  # cheapest, but an outlier
        _quote("retail_2", 48.0, match_quality="normalized"),
        _quote("retail_3", 55.0),
        _quote("retail_4", 52.0),
    ]
    chosen = select_retail(quotes, outliers=["retail_1"])
    assert chosen is not None and chosen.source == "retail_4"


def test_in_stock_beats_cheaper_and_unknown_stock_counts_as_out() -> None:
    quotes = [
        _quote("retail_1", 40.0, in_stock=False),
        _quote("retail_2", 45.0, in_stock=None),  # page did not say
        _quote("retail_3", 50.0, in_stock=True),
    ]
    chosen = select_retail(quotes, outliers=[])
    assert chosen is not None and chosen.source == "retail_3"


def test_nothing_in_stock_falls_back_to_lowest_price() -> None:
    quotes = [_quote("retail_1", 40.0, in_stock=False), _quote("retail_2", 45.0, in_stock=None)]
    chosen = select_retail(quotes, outliers=[])
    assert chosen is not None and chosen.source == "retail_1"


def test_normalized_beats_fuzzy_and_ties_break_by_site() -> None:
    quotes = [
        _quote("retail_1", 30.0, match_quality="fuzzy"),
        _quote("retail_4", 50.0, match_quality="normalized"),
        _quote("retail_2", 50.0, match_quality="normalized"),
    ]
    chosen = select_retail(quotes, outliers=[])
    assert chosen is not None and chosen.source == "retail_2"


def test_all_outliers_selects_nothing() -> None:
    assert select_retail([_quote("retail_1", 30.0)], outliers=["retail_1"]) is None
    assert select_retail([], outliers=[]) is None


# --- build_quotes (pure) --------------------------------------------------------------

CHAIN = ["OLD-1", "MID-2", "NEW-3"]
PAGES = {
    "r2-spelled": RetailPage(
        "retail_2", "AutoDepot Direct | Acme Thermostat new 3 for X\nPrice: $75.00\nIn stock"
    ),
    "r3-old": RetailPage(
        "retail_3", "GearLane | Acme Thermostat MID-2 for X\n$70.00 each\nBackordered"
    ),
    "r4-other": RetailPage(
        "retail_4", "MotorMart Online | Acme Thermostat NEW-4 for X\nPrice: $71.00\nIn stock"
    ),
    "r5-eur": RetailPage(
        "retail_5", "RapidAuto Supply | Acme Thermostat NEW-3\nPrice: EUR 60.00\nIn stock"
    ),
    "r5-noprice": RetailPage(
        "retail_5", "RapidAuto Supply | Acme Thermostat NEW-3\nCall for price"
    ),
    "r1-up": RetailPage("retail_1", "PartsHarbor | NEW-3\nList: $60.00 | Sale: $64.00"),
    "r2-only": RetailPage("retail_2", "AutoDepot Direct | NEW-3\nNow $55.00"),
    "r3-a": RetailPage("retail_3", "GearLane | NEW-3\nPrice: $50.00\nOut of stock"),
    "r3-b": RetailPage("retail_3", "GearLane | new-3\nPrice: $45.00\nIn stock"),
    "r3-c": RetailPage("retail_3", "GearLane | NEW-3\nPrice: $58.00\nIn stock"),
    "r1-sale": RetailPage(
        "retail_1", "PartsHarbor | Acme Thermostat NEW-3 for X\nWas $80.00 Now $68.00\nIn stock"
    ),
}


def _listing(pid: str, listed: str | None, **kw: Any) -> RetailListing:
    return RetailListing(page_id=pid, listed_part_no=listed, **kw)


def test_exact_listing_with_sale_price_computes_discount() -> None:
    listing = _listing(
        "r1-sale",
        "NEW-3",
        list_price=80.0,
        sale_price=68.0,
        in_stock=True,
        price_snippet="Was $80.00 Now $68.00",
    )
    built = build_quotes(CHAIN, [listing], PAGES)
    assert len(built.quotes) == 1
    q = built.quotes[0]
    assert (q.source, q.price_type, q.matched_part_no, q.match_quality) == (
        "retail_1",
        "retail",
        "NEW-3",
        "exact",
    )
    assert (q.list_price, q.sale_price, q.discount_pct, q.effective_price) == (
        80.0,
        68.0,
        15.0,
        68.0,
    )
    assert q.in_stock is True and q.currency == "USD"
    assert [(e.source, e.ref, e.snippet) for e in q.evidence] == [
        ("retail_1", "r1-sale", "Was $80.00 Now $68.00")
    ]


def test_match_quality_by_part_number_comparison() -> None:
    listings = [
        _listing("r2-spelled", "new 3", list_price=75.0, in_stock=True),
        _listing("r3-old", "MID-2", list_price=70.0, in_stock=False),
        _listing("r4-other", "NEW-4", list_price=71.0, in_stock=True),
    ]
    built = build_quotes(CHAIN, listings, PAGES)
    by_site = {q.source: q for q in built.quotes}
    assert sorted(by_site) == ["retail_2", "retail_3"]
    assert by_site["retail_2"].match_quality == "normalized"
    assert by_site["retail_2"].matched_part_no == "new 3"
    assert by_site["retail_3"].match_quality == "fuzzy"
    assert "superseded number MID-2" in by_site["retail_3"].reasoning
    assert [(d.page_id, d.reason) for d in built.rejected] == [
        ("r4-other", "listed part number NEW-4 is not on the supersession chain")
    ]


def test_ungrounded_non_usd_and_unknown_pages_are_dropped() -> None:
    listings = [
        _listing("r1-sale", "NEW-3", list_price=80.0, sale_price=66.0),  # 66 not on page
        _listing("r2-spelled", "NEW-3X", list_price=75.0),  # number not on chain
        _listing("r4-other", "NEW-3", list_price=71.0),  # NEW-3 not written on page
        _listing("r5-eur", "NEW-3", list_price=60.0, currency="eur"),
        _listing("r5-noprice", "NEW-3"),
        _listing("missing", "NEW-3", list_price=10.0),
    ]
    built = build_quotes(CHAIN, listings, PAGES)
    assert built.quotes == []
    assert [(d.page_id, d.reason) for d in built.rejected] == [
        ("r1-sale", "price 66.0 not found in page text"),
        ("r2-spelled", "listed part number NEW-3X is not on the supersession chain"),
        ("r4-other", "listed part number NEW-3 not found in page text"),
        ("r5-eur", "non-USD price (EUR); only USD is used"),
        ("r5-noprice", "page shows no price"),
        ("missing", "unknown page_id"),
    ]


def test_missing_currency_counts_as_usd() -> None:
    listing = _listing("r2-spelled", "new 3", list_price=75.0, currency=None)
    built = build_quotes(CHAIN, [listing], PAGES)
    assert [q.currency for q in built.quotes] == ["USD"]


def test_sale_not_below_list_is_ignored_and_sale_only_has_no_list() -> None:
    listings = [
        _listing("r1-up", "NEW-3", list_price=60.0, sale_price=64.0, in_stock=True),
        _listing("r2-only", "NEW-3", sale_price=55.0),
    ]
    by_site = {q.source: q for q in build_quotes(CHAIN, listings, PAGES).quotes}
    up, only = by_site["retail_1"], by_site["retail_2"]
    assert (up.list_price, up.sale_price, up.discount_pct, up.effective_price) == (
        60.0,
        None,
        None,
        60.0,
    )
    assert (only.list_price, only.sale_price, only.discount_pct, only.effective_price) == (
        None,
        55.0,
        None,
        55.0,
    )
    assert only.in_stock is None
    assert "page does not state stock status" in only.reasoning


def test_one_quote_per_site_best_match_then_stock_then_price() -> None:
    listings = [
        _listing("r3-a", "NEW-3", list_price=50.0, in_stock=False),
        _listing("r3-b", "new-3", list_price=45.0, in_stock=True),  # normalized only
        _listing("r3-c", "NEW-3", list_price=58.0, in_stock=True),
    ]
    built = build_quotes(CHAIN, listings, PAGES)
    assert [(q.source, q.evidence[0].ref) for q in built.quotes] == [("retail_3", "r3-c")]
    assert "3 matching pages on retail_3; chose r3-c" in built.quotes[0].reasoning
    assert sorted((d.page_id, d.reason) for d in built.rejected) == [
        ("r3-a", "another page on retail_3 was chosen (r3-c)"),
        ("r3-b", "another page on retail_3 was chosen (r3-c)"),
    ]


# --- subgraph on generated pages ------------------------------------------------------


@pytest.fixture(scope="module")
def truth(generated_dataset: Path) -> dict[int, GroundTruthRow]:
    return {t.row_id: t for t in load_truth(generated_dataset)}


def _oe_from_truth(t: GroundTruthRow) -> OEResolution:
    """Upstream input built from ground truth so these tests isolate retail_price."""
    status = t.oe_status_expected
    return OEResolution(
        row_id=t.row_id,
        status=status,
        current_oe=t.primary_oe if status == "resolved" else None,
        supersession_chain=t.supersession_chain if status == "resolved" else [],
        candidates=[],
        reasoning="from truth",
    )


@pytest.fixture
def ctx(settings: Settings, generated_dataset: Path) -> RetailContext:
    return RetailContext(tools=build_tools(generated_dataset, settings.tools), settings=settings)


async def test_all_rows_match_truth(ctx: RetailContext, truth: dict[int, GroundTruthRow]) -> None:
    results = await asyncio.gather(
        *(resolve_retail(_oe_from_truth(t), ctx) for t in truth.values())
    )
    for result in results:
        assert isinstance(result, RetailResult), result
        t = truth[result.row_id]
        if t.oe_status_expected != "resolved":
            assert result.quotes == [] and result.selected is None
            continue
        assert result.oe_part_no == t.primary_oe
        want = {site: lst for site, lst in t.retail.sites.items() if lst is not None}
        got = {q.source: q for q in result.quotes}
        assert sorted(got) == sorted(want), result.row_id
        for site, listing in want.items():
            q = got[site]
            assert q.effective_price == pytest.approx(listing.effective_price), (
                result.row_id,
                site,
            )
            assert q.list_price == pytest.approx(listing.list_price)
            assert q.in_stock == listing.in_stock
            assert q.match_quality == listing.match_quality, (result.row_id, site)
        assert sorted(result.outliers) == sorted(t.retail.outlier_sites), result.row_id
        selected = result.selected.source if result.selected else None
        assert selected == t.retail.expected_selected_site, result.row_id


class _CountingTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def search(self, query: str) -> list[ToolHit]:
        self.calls += 1
        return []


class _TimeoutTool:
    def __init__(self, name: str) -> None:
        self.name = name

    async def search(self, query: str) -> list[ToolHit]:
        raise ToolTimeoutError(f"{self.name} timed out")


async def test_short_circuits_without_tool_or_llm_calls(
    settings: Settings, truth: dict[int, GroundTruthRow]
) -> None:
    from rfq_agent.store.db import connect

    tools = {f"retail_search_{i}": _CountingTool(f"retail_search_{i}") for i in range(1, 6)}
    ctx = RetailContext(tools=tools, settings=settings, run_id="retail-short-circuit")
    multiple = next(t for t in truth.values() if t.oe_status_expected == "multiple")
    not_found = next(t for t in truth.values() if t.oe_status_expected == "not_found")

    result = await resolve_retail(_oe_from_truth(multiple), ctx)
    assert isinstance(result, RetailResult)
    assert result.quotes == [] and result.oe_part_no is None
    assert "not uniquely determined" in result.reasoning

    nothing = await resolve_retail(_oe_from_truth(not_found), ctx)
    assert isinstance(nothing, RetailResult) and nothing.quotes == []

    failed = await resolve_retail(RowFailure(row_id=5, stage="search", error="boom"), ctx)
    assert isinstance(failed, RowFailure) and failed.stage == "upstream_oe"

    assert sum(tool.calls for tool in tools.values()) == 0
    with connect(settings.db_path) as conn:
        (llm_calls,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE rfq_run_id = 'retail-short-circuit'"
        ).fetchone()
    conn.close()
    assert llm_calls == 0


def _resolved_with_all_sites(truth: dict[int, GroundTruthRow]) -> GroundTruthRow:
    return next(
        t
        for t in truth.values()
        if t.oe_status_expected == "resolved" and all(t.retail.sites.values())
    )


async def test_one_site_timeout_only_loses_that_site(
    settings: Settings, generated_dataset: Path, truth: dict[int, GroundTruthRow]
) -> None:
    tools: dict[str, Any] = dict(build_tools(generated_dataset, settings.tools))
    tools["retail_search_3"] = _TimeoutTool("retail_search_3")
    ctx = RetailContext(tools=tools, settings=settings)
    result = await resolve_retail(_oe_from_truth(_resolved_with_all_sites(truth)), ctx)
    assert isinstance(result, RetailResult)
    assert sorted(q.source for q in result.quotes) == [
        "retail_1",
        "retail_2",
        "retail_4",
        "retail_5",
    ]
    assert "retail_3 search failed" in result.reasoning


async def test_all_sites_failing_is_row_failure(
    settings: Settings, truth: dict[int, GroundTruthRow]
) -> None:
    tools = {f"retail_search_{i}": _TimeoutTool(f"retail_search_{i}") for i in range(1, 6)}
    ctx = RetailContext(tools=tools, settings=settings)
    result = await resolve_retail(_oe_from_truth(_resolved_with_all_sites(truth)), ctx)
    assert isinstance(result, RowFailure) and result.stage == "search_sites"


@pytest.fixture
def broken_retail_extraction() -> Iterator[None]:
    from rfq_agent.agents import fake_handlers

    @register_fake_handler("RetailListingExtraction")
    def handler(request: FakeRequest) -> dict[str, Any]:
        return {"listings": "nope"}

    yield
    register_fake_handler("RetailListingExtraction")(fake_handlers.fake_retail)


@pytest.mark.usefixtures("broken_retail_extraction")
async def test_extraction_failure_is_row_failure(
    ctx: RetailContext, truth: dict[int, GroundTruthRow]
) -> None:
    result = await resolve_retail(_oe_from_truth(_resolved_with_all_sites(truth)), ctx)
    assert isinstance(result, RowFailure) and result.stage == "extract_listings"


async def test_outlier_row_explains_outlier(
    ctx: RetailContext, truth: dict[int, GroundTruthRow]
) -> None:
    t = next(
        t for t in truth.values() if t.retail.outlier_sites and t.oe_status_expected == "resolved"
    )
    result = await resolve_retail(_oe_from_truth(t), ctx)
    assert isinstance(result, RetailResult)
    site = t.retail.outlier_sites[0]
    assert f"{site} " in result.reasoning and "(outlier)" in result.reasoning
    assert f"selected {t.retail.expected_selected_site}" in result.reasoning


# --- agreement with the ground-truth selection ------------------------------------------


def test_select_retail_agrees_with_ground_truth_rule(truth: dict[int, GroundTruthRow]) -> None:
    """The agent's rule and the generator's rule are separate code; they must agree."""
    checked = 0
    for t in truth.values():
        quotes = [
            _quote(
                site,
                listing.effective_price,
                in_stock=listing.in_stock,
                match_quality=listing.match_quality,
            )
            for site, listing in t.retail.sites.items()
            if listing is not None
        ]
        chosen = select_retail(quotes, t.retail.outlier_sites)
        assert (chosen.source if chosen else None) == t.retail.expected_selected_site, t.row_id
        checked += bool(quotes)
    assert checked > 100


# --- comparison table (manual review aid) -------------------------------------------------


def _result_from_truth(t: GroundTruthRow) -> RetailResult:
    quotes = [
        _quote(
            site,
            listing.effective_price,
            list_price=listing.list_price,
            in_stock=listing.in_stock,
            match_quality=listing.match_quality,
        )
        for site, listing in t.retail.sites.items()
        if listing is not None
    ]
    selected = next((q for q in quotes if q.source == t.retail.expected_selected_site), None)
    return RetailResult(
        row_id=t.row_id,
        oe_part_no=t.primary_oe,
        quotes=quotes,
        selected=selected,
        outliers=list(t.retail.outlier_sites),
    )


def test_compare_retail_checks_six_items(truth: dict[int, GroundTruthRow]) -> None:
    from rfq_agent.eval.quick_compare import compare_retail, render_retail_markdown

    t = next(
        t
        for t in truth.values()
        if t.oe_status_expected == "resolved"
        and t.retail.outlier_sites
        and sum(lst is not None for lst in t.retail.sites.values()) >= 4
    )
    multiple = next(t for t in truth.values() if t.oe_status_expected == "multiple")
    good = _result_from_truth(t)

    wrong = _result_from_truth(t)
    normal = [q for q in wrong.quotes if q.source not in t.retail.outlier_sites]
    off, flipped, other = normal[0], normal[1], normal[2]
    off.effective_price = round((off.effective_price or 0) * 1.02, 2)  # 2% > 1% tolerance
    flipped.in_stock = not flipped.in_stock
    other.match_quality = "fuzzy"
    wrong.outliers = []
    wrong.selected = next(q for q in wrong.quotes if q.source != t.retail.expected_selected_site)
    missing_site = _result_from_truth(t)
    missing_site.quotes = missing_site.quotes[1:]

    skipped = RetailResult(
        row_id=multiple.row_id, oe_part_no=None, quotes=[], selected=None, outliers=[]
    )
    failure = RowFailure(row_id=t.row_id, stage="search_sites", error="boom")

    [ok] = compare_retail([good], truth.values())
    assert (ok.sites_ok, ok.price_ok, ok.stock_ok, ok.match_ok, ok.outliers_ok, ok.selected_ok) == (
        True,
        True,
        True,
        True,
        True,
        True,
    )
    [bad] = compare_retail([wrong], truth.values())
    assert (bad.sites_ok, bad.price_ok, bad.stock_ok, bad.match_ok) == (True, False, False, False)
    assert (bad.outliers_ok, bad.selected_ok) == (False, False)
    assert (bad.prices_correct, bad.prices_total) == (len(wrong.quotes) - 1, len(wrong.quotes))
    [gap] = compare_retail([missing_site], truth.values())
    assert gap.sites_ok is False
    [skip] = compare_retail([skipped], truth.values())
    assert skip.skipped and skip.correct
    [fail] = compare_retail([failure], truth.values())
    assert fail.failed and not fail.correct

    report = render_retail_markdown([ok, bad, skip, fail])
    assert report.splitlines()[-1] == (
        f"**Summary:** retail correct 2/4 · site prices "
        # a failed row still counts its expected sites in the denominator
        f"{2 * len(good.quotes) - 1}/{3 * len(good.quotes)} · outliers correct 2/4 · "
        "selected correct 2/4 · OE-skipped rows 1 · failed rows 1"
    )


# --- CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_config(config_dir: Path, generated_dataset: Path) -> Path:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["data"]["dataset_dir"] = str(generated_dataset)
    path.write_text(yaml.safe_dump(data))
    return config_dir


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.usefixtures("cli_config")
def test_cli_from_input_runs_both_agents(generated_dataset: Path, tmp_path: Path) -> None:
    out = tmp_path / "retail.jsonl"
    sample = str(generated_dataset / "rfq_sample_20.xlsx")
    result = CliRunner().invoke(
        app, ["agent", "retail-price", "--input", sample, "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    records = _records(out)
    assert [r["row_id"] for r in records] == list(range(1, 21))
    assert all(r["ok"] for r in records)
    RetailResult.model_validate(records[0]["result"])
    assert out.with_suffix(".oe.jsonl").is_file()
    assert "retail correct 20/20" in out.with_suffix(".compare.md").read_text()
    trace = _records(out.with_suffix(".trace.jsonl"))
    assert {e["agent"] for e in trace} == {"oe_interchange", "retail_price"}
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["agent"] == "retail_price" and meta["input"].endswith("rfq_sample_20.xlsx")


@pytest.mark.usefixtures("cli_config")
def test_cli_from_oe_reuses_upstream(generated_dataset: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    oe_out = tmp_path / "oe.jsonl"
    sample = str(generated_dataset / "rfq_sample_20.xlsx")
    first = runner.invoke(
        app, ["agent", "oe-interchange", "--input", sample, "--output", str(oe_out)]
    )
    assert first.exit_code == 0, first.output
    out = tmp_path / "retail.jsonl"
    result = runner.invoke(
        app, ["agent", "retail-price", "--from-oe", str(oe_out), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "retail correct 20/20" in out.with_suffix(".compare.md").read_text()
    trace = _records(out.with_suffix(".trace.jsonl"))
    assert {e["agent"] for e in trace} == {"retail_price"}


@pytest.mark.usefixtures("cli_config")
def test_cli_refuses_real_provider_without_flag(
    generated_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RFQ_LLM_PROVIDER")
    result = CliRunner().invoke(
        app, ["agent", "retail-price", "--input", str(generated_dataset / "rfq_sample_20.xlsx")]
    )
    assert result.exit_code == 2
    assert "--allow-real" in result.output


# --- review fixes -----------------------------------------------------------------------

WARRANTY_PAGE = {
    "r1-warranty": RetailPage(
        "retail_1",
        "PartsHarbor | Acme Thermostat 0B7-379-712J for X\nPrice: $25.75\nIn stock\n"
        "Warranty: 12 months / 12,000 miles.",
    )
}


@pytest.mark.parametrize(
    ("list_price", "sale_price"),
    [(12000.0, None), (712.0, None), (25.75, 12.0), (379.0, None)],
)
def test_prices_must_appear_as_money_not_inside_part_numbers_or_units(
    list_price: float, sale_price: float | None
) -> None:
    listing = _listing("r1-warranty", "0B7-379-712J", list_price=list_price, sale_price=sale_price)
    built = build_quotes(["0B7-379-712J"], [listing], WARRANTY_PAGE)
    assert built.quotes == []
    assert "not found in page text" in built.rejected[0].reason


@pytest.mark.parametrize(
    "line",
    [
        "Price: $25.75",
        "$25.75 each",
        "Reg. 25.75 USD",
        "List price (MSRP) USD 25.75",
        "Price: 25.75",
    ],
)
def test_money_formats_are_accepted(line: str) -> None:
    pages = {"p": RetailPage("retail_1", f"Shop | NEW-3\n{line}\nIn stock")}
    built = build_quotes(CHAIN, [_listing("p", "NEW-3", list_price=25.75)], pages)
    assert [q.effective_price for q in built.quotes] == [25.75]


def test_non_positive_price_is_dropped() -> None:
    pages = {"p": RetailPage("retail_1", "Shop | NEW-3\nPrice: $0.00\nIn stock")}
    built = build_quotes(CHAIN, [_listing("p", "NEW-3", list_price=0.0)], pages)
    assert built.quotes == []
    assert built.rejected[0].reason == "non-positive price 0.0"


def test_zero_median_skips_outlier_detection() -> None:
    quotes = [_quote("retail_1", 0.0), _quote("retail_2", 0.0), _quote("retail_3", 5.0)]
    check = detect_outliers(quotes, 0.40)
    assert check.outliers == [] and check.median is None


def test_all_outliers_reasoning_asks_for_manual_review() -> None:
    from rfq_agent.agents.retail_price import row_reasoning

    quotes = [_quote(f"retail_{i}", p) for i, p in enumerate((10.0, 10.0, 100.0, 100.0), 1)]
    check = detect_outliers(quotes, 0.40)
    assert len(check.outliers) == 4
    reasoning = row_reasoning(quotes, check, select_retail(quotes, check.outliers), {}, "")
    assert "no reliable reference price; needs manual review" in reasoning


def test_discount_rounds_half_up() -> None:
    # (20.00 - 17.53) / 20.00 = 12.35% exactly; float round() would give 12.3.
    pages = {"p": RetailPage("retail_1", "Shop | NEW-3\nWas $20.00 Now $17.53\nIn stock")}
    listing = _listing("p", "NEW-3", list_price=20.0, sale_price=17.53, in_stock=True)
    [quote] = build_quotes(CHAIN, [listing], pages).quotes
    assert quote.discount_pct == 12.4


async def test_exact_is_judged_against_current_oe_spelling(
    ctx: RetailContext, truth: dict[int, GroundTruthRow]
) -> None:
    t = next(
        t
        for t in truth.values()
        if t.oe_status_expected == "resolved"
        and t.primary_oe != normalize_part_no(t.primary_oe or "")
        and any(lst and lst.match_quality == "exact" for lst in t.retail.sites.values())
    )
    assert t.primary_oe is not None
    upstream = _oe_from_truth(t)
    # Chain tail spelled differently from current_oe (same number after normalization).
    upstream.supersession_chain = [*t.supersession_chain[:-1], normalize_part_no(t.primary_oe)]
    result = await resolve_retail(upstream, ctx)
    assert isinstance(result, RetailResult)
    exact_sites = {s for s, lst in t.retail.sites.items() if lst and lst.match_quality == "exact"}
    assert {q.source for q in result.quotes if q.match_quality == "exact"} == exact_sites


class _FailOnQuery:
    """Wraps a real tool but times out for one query."""

    def __init__(self, tool: Any, bad_query: str) -> None:
        self.name, self._tool, self._bad = tool.name, tool, bad_query

    async def search(self, query: str) -> list[ToolHit]:
        if query == self._bad:
            raise ToolTimeoutError(f"{self.name} timed out")
        hits: list[ToolHit] = await self._tool.search(query)
        return hits


async def test_site_failing_on_one_chain_number_counts_as_failed(
    settings: Settings, generated_dataset: Path, truth: dict[int, GroundTruthRow]
) -> None:
    t = next(
        t
        for t in truth.values()
        if t.oe_status_expected == "resolved"
        and len(t.supersession_chain) >= 2
        and t.retail.sites.get("retail_2") is not None
    )
    tools: dict[str, Any] = dict(build_tools(generated_dataset, settings.tools))
    # The listing is indexed under the current number; only the older number's search fails.
    tools["retail_search_2"] = _FailOnQuery(tools["retail_search_2"], t.supersession_chain[0])
    result = await resolve_retail(_oe_from_truth(t), RetailContext(tools=tools, settings=settings))
    assert isinstance(result, RetailResult)
    assert "retail_2" not in {q.source for q in result.quotes}
    assert "retail_2 search failed" in result.reasoning
