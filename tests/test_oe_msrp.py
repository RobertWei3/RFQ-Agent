"""oe_msrp tests: pure selection rules, subgraph against generated pages, CLI."""

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
from rfq_agent.agents.oe_msrp import MSRPContext, MSRPPage, resolve_msrp, select_msrp
from rfq_agent.cli import app
from rfq_agent.config import FaultSettings, Settings, ToolSettings
from rfq_agent.data_gen.audit import load_truth
from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.llm.fake import FakeRequest, register_fake_handler
from rfq_agent.schemas import MSRPResult, OEResolution
from rfq_agent.tools import build_tools

# --- select_msrp (pure) ---------------------------------------------------------------


def _page(pid: str, subject: str | None, price: float | None, **kw: Any) -> MSRPPage:
    return MSRPPage(page_id=pid, subject_part_no=subject, price=price, **kw)


TEXTS = {
    "p-new": "Part: NEW-3\nMSRP: $120.50 USD",
    "p-mid": "Part: MID-2\nManufacturer's suggested retail price $1,099.00",
    "p-old": "Part OLD-1 is no longer sold under this number. Please order MID-2.",
    "p-new2": "Part: NEW-3\nList price (MSRP) USD 130.00\nThis item is discontinued.",
    "p-eur": "Part: NEW-3\nPrice EUR 99.00",
    "p-other": "Part: ZZZ-9\nMSRP: $10.00 USD",
}
CHAIN = ["OLD-1", "MID-2", "NEW-3"]


def test_current_number_priced() -> None:
    sel = select_msrp(CHAIN, [_page("p-new", "NEW-3", 120.5, currency="USD")], TEXTS)
    assert sel.status == "found"
    assert sel.quote is not None
    assert sel.quote.effective_price == sel.quote.list_price == 120.5
    assert sel.quote.matched_part_no == "NEW-3" and sel.quote.match_quality == "exact"
    assert sel.quote.price_type == "msrp" and sel.quote.in_stock is None


def test_fallback_to_older_number_skips_redirect_page() -> None:
    pages = [
        _page("p-old", "OLD-1", None),
        _page("p-mid", "MID-2", 1099.0),
    ]
    sel = select_msrp(CHAIN, pages, TEXTS)
    assert sel.status == "found"
    assert sel.quote is not None and sel.quote.matched_part_no == "MID-2"
    assert "fallback" in sel.reasoning and "NEW-3" in sel.reasoning


def test_discontinued_and_conflict_takes_higher() -> None:
    pages = [_page("p-new", "NEW-3", 120.5), _page("p-new2", "NEW-3", 130.0, discontinued=True)]
    sel = select_msrp(CHAIN, pages, TEXTS)
    assert sel.status == "discontinued"
    assert sel.quote is not None and sel.quote.effective_price == 130.0
    assert "conflict" in sel.reasoning
    assert {e.ref for e in sel.quote.evidence} == {"p-new", "p-new2"}


def test_missing_currency_means_usd() -> None:
    sel = select_msrp(CHAIN, [_page("p-new", "NEW-3", 120.5)], TEXTS)
    assert sel.quote is not None and sel.quote.currency == "USD"
    assert "currency" not in sel.reasoning


def test_non_usd_price_not_adopted_and_falls_back() -> None:
    pages = [
        _page("p-eur", "NEW-3", 99.0, currency="eur"),
        _page("p-mid", "MID-2", 1099.0, currency="USD"),
    ]
    sel = select_msrp(CHAIN, pages, TEXTS)
    assert sel.status == "found"
    assert sel.quote is not None and sel.quote.matched_part_no == "MID-2"
    assert sel.quote.currency == "USD"
    assert "non-USD price (EUR) not adopted" in sel.reasoning
    assert any(d.page_id == "p-eur" and "non-USD" in d.reason for d in sel.dropped)


def test_only_non_usd_price_means_not_found() -> None:
    sel = select_msrp(["NEW-3"], [_page("p-eur", "NEW-3", 99.0, currency="EUR")], TEXTS)
    assert sel.status == "not_found" and sel.quote is None


def test_discontinued_taken_from_the_chosen_page_only() -> None:
    # The lower-priced page says discontinued; the higher-priced page (chosen) does not.
    texts = {
        "a": "Part: NEW-3\nMSRP: $100.00 USD\nThis item is discontinued.",
        "b": "Part: NEW-3\nMSRP: $140.00 USD",
    }
    pages = [_page("a", "NEW-3", 100.0, discontinued=True), _page("b", "NEW-3", 140.0)]
    sel = select_msrp(["NEW-3"], pages, texts)
    assert sel.quote is not None and sel.quote.effective_price == 140.0
    assert sel.status == "found"
    assert "discontinued" not in sel.reasoning.split("price conflict")[0]


def test_reasoning_distinguishes_unverified_price_from_missing_page() -> None:
    pages = [_page("p-new", "NEW-3", 999.0), _page("p-mid", "MID-2", 1099.0)]
    sel = select_msrp(CHAIN, pages, TEXTS)
    assert sel.quote is not None and sel.quote.matched_part_no == "MID-2"
    assert "has no price page" not in sel.reasoning
    assert "NEW-3 price could not be verified in the page text" in sel.reasoning

    sel2 = select_msrp(CHAIN, [_page("p-mid", "MID-2", 1099.0)], TEXTS)
    assert "current OE NEW-3 has no price page" in sel2.reasoning


def test_fallback_reasoning_states_each_fact_once() -> None:
    sel = select_msrp(CHAIN, [_page("p-mid", "MID-2", 1099.0)], TEXTS)
    assert sel.reasoning.count("NEW-3") == 1
    assert "used MSRP of older number MID-2 (supersession fallback)" in sel.reasoning


def test_discontinued_evidence_includes_the_discontinued_line() -> None:
    page = _page(
        "p-new2",
        "NEW-3",
        130.0,
        discontinued=True,
        snippet="List price (MSRP) USD 130.00",
        discontinued_snippet="This item is discontinued.",
    )
    sel = select_msrp(CHAIN, [page], TEXTS)
    assert sel.status == "discontinued" and sel.quote is not None
    snippets = [(e.ref, e.snippet) for e in sel.quote.evidence]
    assert ("p-new2", "List price (MSRP) USD 130.00") in snippets
    assert ("p-new2", "This item is discontinued.") in snippets


def test_ungrounded_price_and_foreign_pages_dropped() -> None:
    pages = [
        _page("p-new", "NEW-3", 999.0),  # price not on the page
        _page("p-other", "ZZZ-9", 10.0),  # not on the chain
        _page("p-missing", "NEW-3", 1.0),  # unknown page id
    ]
    sel = select_msrp(CHAIN, pages, TEXTS)
    assert sel.status == "not_found" and sel.quote is None
    reasons = {d.page_id: d.reason for d in sel.dropped}
    assert "not found in page text" in reasons["p-new"]
    assert "not on the supersession chain" in reasons["p-other"]
    assert reasons["p-missing"] == "unknown page_id"


def test_price_must_appear_as_money() -> None:
    # 12000 only appears as "12,000 miles"; 379 only inside the part number.
    texts = {"p": "Part: 0B7-379-712J\nMSRP: $45.10\nWarranty 12,000 miles"}
    for fake in (12000.0, 379.0):
        sel = select_msrp(["0B7-379-712J"], [_page("p", "0B7-379-712J", fake)], texts)
        assert sel.status == "not_found", fake
        assert "not found in page text" in sel.dropped[0].reason


def test_zero_msrp_not_adopted_and_falls_back() -> None:
    texts = {"p-new": "Part: NEW-3\nMSRP: $0.00", "p-mid": "Part: MID-2\nMSRP: $95.00"}
    pages = [_page("p-new", "NEW-3", 0.0), _page("p-mid", "MID-2", 95.0)]
    sel = select_msrp(CHAIN, pages, texts)
    assert sel.quote is not None and sel.quote.matched_part_no == "MID-2"
    assert "NEW-3 non-positive price" in sel.reasoning


def test_normalized_match_quality() -> None:
    texts = {"p": "Part: new 3\nMSRP: $5.00 USD"}
    sel = select_msrp(["NEW-3"], [_page("p", "new 3", 5.0)], texts)
    assert sel.quote is not None and sel.quote.match_quality == "normalized"


# --- subgraph on generated pages ------------------------------------------------------


@pytest.fixture(scope="module")
def truth(generated_dataset: Path) -> dict[int, GroundTruthRow]:
    return {t.row_id: t for t in load_truth(generated_dataset)}


def _oe_from_truth(t: GroundTruthRow) -> OEResolution:
    """Upstream input built from ground truth so these tests isolate oe_msrp."""
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
def ctx(settings: Settings, generated_dataset: Path) -> MSRPContext:
    return MSRPContext(tools=build_tools(generated_dataset, settings.tools), settings=settings)


async def test_all_rows_match_truth(ctx: MSRPContext, truth: dict[int, GroundTruthRow]) -> None:
    results = await asyncio.gather(*(resolve_msrp(_oe_from_truth(t), ctx) for t in truth.values()))
    for result in results:
        assert isinstance(result, MSRPResult), result
        t = truth[result.row_id]
        if t.oe_status_expected != "resolved":
            assert result.status == "not_found" and result.quote is None
            continue
        assert result.status == t.msrp.status, result.row_id
        assert result.oe_part_no == t.primary_oe
        if t.msrp.value is None:
            assert result.quote is None
        else:
            assert result.quote is not None
            assert result.quote.effective_price == pytest.approx(t.msrp.value)
            assert result.quote.matched_part_no == t.msrp.source_oe
        if result.status == "discontinued":
            assert result.quote is not None
            assert any(
                "discontinued" in (e.snippet or "").lower() for e in result.quote.evidence
            ), result.row_id


async def test_fallback_rows_explain_fallback(
    ctx: MSRPContext, truth: dict[int, GroundTruthRow]
) -> None:
    # Difficulties may overlap; rows whose OE is "multiple" are never looked up (P3-Q2).
    rows = [
        t
        for t in truth.values()
        if "msrp_fallback" in t.tags and t.oe_status_expected == "resolved"
    ]
    assert rows
    for t in rows:
        result = await resolve_msrp(_oe_from_truth(t), ctx)
        assert isinstance(result, MSRPResult)
        assert "fallback" in result.reasoning
        assert result.quote is not None and result.quote.matched_part_no != t.primary_oe


class _CountingTool:
    name = "msrp_lookup"

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str) -> list[Any]:
        self.calls += 1
        return []


async def test_short_circuits_without_tool_or_llm_calls(
    settings: Settings, truth: dict[int, GroundTruthRow]
) -> None:
    from rfq_agent.store.db import connect

    tool = _CountingTool()
    ctx = MSRPContext(tools={"msrp_lookup": tool}, settings=settings, run_id="short-circuit")
    multiple = next(t for t in truth.values() if t.oe_status_expected == "multiple")
    not_found = next(t for t in truth.values() if t.oe_status_expected == "not_found")

    result = await resolve_msrp(_oe_from_truth(multiple), ctx)
    assert isinstance(result, MSRPResult)
    assert result.status == "not_found" and result.oe_part_no is None
    assert "not uniquely determined" in result.reasoning

    nothing = await resolve_msrp(_oe_from_truth(not_found), ctx)
    assert isinstance(nothing, MSRPResult) and nothing.status == "not_found"

    failed = await resolve_msrp(RowFailure(row_id=5, stage="search", error="boom"), ctx)
    assert isinstance(failed, RowFailure) and failed.stage == "upstream_oe"
    assert "boom" in failed.error

    assert tool.calls == 0
    with connect(settings.db_path) as conn:
        (llm_calls,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE rfq_run_id = 'short-circuit'"
        ).fetchone()
    conn.close()
    assert llm_calls == 0


async def test_tool_timeout_is_row_failure(
    settings: Settings, generated_dataset: Path, truth: dict[int, GroundTruthRow]
) -> None:
    faults = FaultSettings(enabled=True, timeout_rate=1.0)
    ctx = MSRPContext(
        tools=build_tools(generated_dataset, ToolSettings(faults=faults)), settings=settings
    )
    t = next(t for t in truth.values() if t.oe_status_expected == "resolved")
    result = await resolve_msrp(_oe_from_truth(t), ctx)
    assert isinstance(result, RowFailure) and result.stage == "search_chain"


@pytest.fixture
def broken_msrp_extraction() -> Iterator[None]:
    from rfq_agent.agents import fake_handlers

    @register_fake_handler("MSRPPageExtraction")
    def handler(request: FakeRequest) -> dict[str, Any]:
        return {"pages": "nope"}

    yield
    register_fake_handler("MSRPPageExtraction")(fake_handlers.fake_msrp)


@pytest.mark.usefixtures("broken_msrp_extraction")
async def test_extraction_failure_is_row_failure(
    ctx: MSRPContext, truth: dict[int, GroundTruthRow]
) -> None:
    t = next(t for t in truth.values() if t.oe_status_expected == "resolved")
    result = await resolve_msrp(_oe_from_truth(t), ctx)
    assert isinstance(result, RowFailure) and result.stage == "extract_prices"


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
    out = tmp_path / "msrp.jsonl"
    result = CliRunner().invoke(
        app,
        [
            "agent",
            "oe-msrp",
            "--input",
            str(generated_dataset / "rfq_sample_20.xlsx"),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    records = _records(out)
    assert [r["row_id"] for r in records] == list(range(1, 21))
    assert all(r["ok"] for r in records)
    MSRPResult.model_validate(records[0]["result"])
    assert out.with_suffix(".oe.jsonl").is_file()  # upstream OE results kept
    report = out.with_suffix(".compare.md").read_text()
    assert "| status | price ±1% | source |" in report
    assert "MSRP correct 20/20" in report
    trace = _records(out.with_suffix(".trace.jsonl"))
    assert {e["agent"] for e in trace} == {"oe_interchange", "oe_msrp"}
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["agent"] == "oe_msrp" and meta["input"].endswith("rfq_sample_20.xlsx")


@pytest.mark.usefixtures("cli_config")
def test_cli_from_oe_reuses_upstream_and_finds_truth_via_meta(
    generated_dataset: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    oe_out = tmp_path / "oe.jsonl"
    first = runner.invoke(
        app,
        [
            "agent",
            "oe-interchange",
            "--input",
            str(generated_dataset / "rfq_sample_20.xlsx"),
            "--output",
            str(oe_out),
        ],
    )
    assert first.exit_code == 0, first.output
    assert json.loads(oe_out.with_suffix(".meta.json").read_text())["agent"] == "oe_interchange"

    out = tmp_path / "msrp.jsonl"
    result = runner.invoke(
        app, ["agent", "oe-msrp", "--from-oe", str(oe_out), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert len(_records(out)) == 20
    assert "MSRP correct 20/20" in out.with_suffix(".compare.md").read_text()
    trace = _records(out.with_suffix(".trace.jsonl"))
    assert {e["agent"] for e in trace} == {"oe_msrp"}  # no oe_interchange re-run


@pytest.mark.usefixtures("cli_config")
def test_cli_requires_exactly_one_input(tmp_path: Path) -> None:
    neither = CliRunner().invoke(app, ["agent", "oe-msrp", "--output", str(tmp_path / "x.jsonl")])
    assert neither.exit_code != 0 and "exactly one" in neither.output
    both = CliRunner().invoke(
        app, ["agent", "oe-msrp", "--input", "a.xlsx", "--from-oe", "b.jsonl"]
    )
    assert both.exit_code != 0 and "exactly one" in both.output


@pytest.mark.usefixtures("cli_config")
def test_cli_refuses_real_provider_without_flag(
    generated_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RFQ_LLM_PROVIDER")
    result = CliRunner().invoke(
        app, ["agent", "oe-msrp", "--input", str(generated_dataset / "rfq_sample_20.xlsx")]
    )
    assert result.exit_code == 2
    assert "--allow-real" in result.output
