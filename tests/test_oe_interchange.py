"""oe_interchange subgraph tests in fake mode against the generated dataset."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rfq_agent.agents.common import RowFailure, page_mentions
from rfq_agent.agents.oe_interchange import (
    ChainError,
    OEContext,
    build_chains,
    resolve_line,
)
from rfq_agent.config import FaultSettings, Settings, ToolSettings
from rfq_agent.data_gen.audit import load_truth
from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.intake import read_rfq
from rfq_agent.llm.fake import FakeRequest, register_fake_handler
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import OEResolution, RFQLine
from rfq_agent.tools import build_tools

# --- pure helpers -------------------------------------------------------------------


def test_build_chains_both_directions() -> None:
    edges = [("A", "B"), ("B", "C")]
    assert build_chains(edges, ["A", "B", "C"]) == {
        "A": ["A", "B", "C"],
        "B": ["A", "B", "C"],
        "C": ["A", "B", "C"],
    }
    assert build_chains([], ["X"]) == {"X": ["X"]}
    assert build_chains([("A", "B"), ("A", "B")], ["A"]) == {"A": ["A", "B"]}


@pytest.mark.parametrize(
    ("edges", "match"),
    [
        ([("A", "B"), ("B", "A")], "both|cycle"),
        ([("A", "B"), ("B", "C"), ("C", "A")], "both|cycle"),
        ([("A", "B"), ("A", "C")], "superseded by both"),
        ([("A", "C"), ("B", "C")], "replaces both"),
    ],
)
def test_build_chains_errors(edges: list[tuple[str, str]], match: str) -> None:
    with pytest.raises(ChainError, match=match):
        build_chains(edges, ["A"])


def test_page_mentions() -> None:
    text = "OE# 0B7-379-712J fits\nSuperseded by: 1234.56.789."
    assert page_mentions(text, "0B7379712J")
    assert page_mentions(text, "123456789")
    assert page_mentions("Part 21200 4A7 here", "212004A7")
    assert not page_mentions(text, "0B7379")


def test_page_mentions_number_split_into_several_tokens() -> None:
    # Retail titles may replace every dash with a space (P4).
    assert page_mentions("Valtora Thermostat 6F8 766 376W for Lido", "6F8766376W")
    assert not page_mentions("Valtora Thermostat 6F8 766 for Lido", "6F8766376W")


# --- subgraph against generated pages ----------------------------------------------


@pytest.fixture(scope="module")
def lines(generated_dataset: Path) -> dict[int, RFQLine]:
    return {line.row_id: line for line in read_rfq(generated_dataset / "rfq_200.xlsx").lines}


@pytest.fixture(scope="module")
def truth(generated_dataset: Path) -> dict[int, GroundTruthRow]:
    return {t.row_id: t for t in load_truth(generated_dataset)}


def _xref_lookup(dataset: Path) -> Any:
    conn = sqlite3.connect(dataset / "internal.db")
    xref = dict(conn.execute("SELECT customer_part_no_norm, our_part_no FROM customer_xref"))
    conn.close()

    async def lookup(customer_part_no: str) -> str | None:
        return xref.get(normalize_part_no(customer_part_no))

    return lookup


@pytest.fixture
def ctx(settings: Settings, generated_dataset: Path) -> OEContext:
    return OEContext(
        tools=build_tools(generated_dataset, settings.tools),
        lookup_our_part=_xref_lookup(generated_dataset),
        settings=settings,
        run_id="test",
    )


def _row(truth: dict[int, GroundTruthRow], predicate: Any) -> GroundTruthRow:
    return next(t for t in truth.values() if predicate(t))


async def test_all_rows_resolve_correctly_in_fake_mode(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    results = await asyncio.gather(*(resolve_line(line, ctx) for line in lines.values()))
    for result in results:
        assert isinstance(result, OEResolution), result
        t = truth[result.row_id]
        assert result.status == t.oe_status_expected, result.row_id
        if result.status == "resolved":
            assert result.current_oe == t.primary_oe
            assert result.supersession_chain == t.supersession_chain
        if result.status == "multiple":
            assert result.current_oe is None and result.supersession_chain == []
            assert {c.oe_part_no for c in result.candidates} >= set(t.accepted_oes)
        expected_source = {"given": "given", "xref": "xref", "unreachable": "none"}
        assert result.our_part_source == expected_source[t.our_part_reachability]
        if t.our_part_reachability != "unreachable":
            assert result.our_part_no == t.our_part_no


async def test_supersession_entered_from_head_and_tail(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    chained = [
        t
        for t in truth.values()
        if len(t.supersession_chain) > 1 and t.oe_status_expected == "resolved"
    ]
    entries = set()
    for t in chained:
        result = await resolve_line(lines[t.row_id], ctx)
        assert isinstance(result, OEResolution)
        assert result.supersession_chain == t.supersession_chain
        candidate = result.candidates[0]
        refs = [e.ref for e in candidate.evidence]
        assert len([r for r in refs if r.startswith("cat-")]) == len(t.supersession_chain)
        interchange_text = next(e.snippet for e in candidate.evidence if e.source == "interchange")
        entries.add("head" if t.supersession_chain[0] in (interchange_text or "") else "tail")
    assert entries == {"head", "tail"}


async def test_many_to_one_rows_share_current_oe(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    t = _row(truth, lambda r: r.shares_oe_with)
    a = await resolve_line(lines[t.row_id], ctx)
    b = await resolve_line(lines[t.shares_oe_with[0]], ctx)
    assert isinstance(a, OEResolution) and isinstance(b, OEResolution)
    assert a.current_oe == b.current_oe == t.primary_oe


async def test_not_found_row(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    t = _row(truth, lambda r: r.oe_status_expected == "not_found")
    result = await resolve_line(lines[t.row_id], ctx)
    assert isinstance(result, OEResolution)
    assert result.status == "not_found" and result.candidates == [] and result.current_oe is None


async def test_dirty_input_row(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    t = _row(truth, lambda r: "dirty_input" in r.tags and r.primary_oe)
    result = await resolve_line(lines[t.row_id], ctx)
    assert isinstance(result, OEResolution) and result.status == t.oe_status_expected


@pytest.fixture
def hallucinating_interchange() -> Iterator[None]:
    """Fake that adds an invented candidate and drops nothing."""
    from rfq_agent.agents import fake_handlers

    @register_fake_handler("InterchangeExtraction")
    def handler(request: FakeRequest) -> dict[str, Any]:
        data = fake_handlers.fake_interchange(request)
        if data["candidates"]:
            first = data["candidates"][0]
            data["candidates"].append({**first, "oe_part_no": "INVENTED-999", "brand": "Ghost"})
        return data

    yield
    register_fake_handler("InterchangeExtraction")(fake_handlers.fake_interchange)


@pytest.mark.usefixtures("hallucinating_interchange")
async def test_ungrounded_candidate_is_llm_inferred_and_ignored(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    t = _row(truth, lambda r: r.oe_status_expected == "resolved")
    result = await resolve_line(lines[t.row_id], ctx)
    assert isinstance(result, OEResolution)
    assert result.status == "resolved" and result.current_oe == t.primary_oe
    inferred = [c for c in result.candidates if c.match_type == "llm_inferred"]
    assert [c.oe_part_no for c in inferred] == ["INVENTED-999"]


@pytest.fixture
def cyclic_catalog() -> Iterator[None]:
    from rfq_agent.agents import fake_handlers

    @register_fake_handler("CatalogExtraction")
    def handler(request: FakeRequest) -> dict[str, Any]:
        data = fake_handlers.fake_catalog(request)
        for page in data["pages"]:
            # Every page claims it both replaces and is superseded by itself's neighbour.
            if page["subject_part_no"] and page["superseded_by"] is None and page["replaces"]:
                page["superseded_by"] = page["replaces"]
        return data

    yield
    register_fake_handler("CatalogExtraction")(fake_handlers.fake_catalog)


@pytest.mark.usefixtures("cyclic_catalog")
async def test_cycle_becomes_row_failure(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    t = _row(truth, lambda r: len(r.supersession_chain) == 2 and r.oe_status_expected == "resolved")
    result = await resolve_line(lines[t.row_id], ctx)
    assert isinstance(result, RowFailure)
    assert result.stage in {"build", "expand_chain"}
    assert "cycle" in result.error or "both" in result.error


async def test_tool_timeout_becomes_row_failure(
    settings: Settings, generated_dataset: Path, lines: dict[int, RFQLine]
) -> None:
    faults = FaultSettings(enabled=True, timeout_rate=1.0)
    ctx = OEContext(
        tools=build_tools(generated_dataset, ToolSettings(faults=faults)),
        lookup_our_part=_xref_lookup(generated_dataset),
        settings=settings,
    )
    result = await resolve_line(lines[1], ctx)
    assert isinstance(result, RowFailure) and result.stage == "search"
    assert "ToolTimeoutError" in result.error


@pytest.fixture
def broken_extraction() -> Iterator[None]:
    from rfq_agent.agents import fake_handlers

    @register_fake_handler("InterchangeExtraction")
    def handler(request: FakeRequest) -> dict[str, Any]:
        return {"candidates": "not-a-list"}

    yield
    register_fake_handler("InterchangeExtraction")(fake_handlers.fake_interchange)


@pytest.mark.usefixtures("broken_extraction")
async def test_extraction_failure_is_isolated_per_row(
    ctx: OEContext, lines: dict[int, RFQLine], truth: dict[int, GroundTruthRow]
) -> None:
    t = _row(truth, lambda r: r.oe_status_expected == "resolved")
    results = await asyncio.gather(
        resolve_line(lines[t.row_id], ctx), resolve_line(lines[t.row_id], ctx)
    )
    for result in results:
        assert isinstance(result, RowFailure) and result.stage == "extract_candidates"


async def test_lookup_failure_is_row_failure(ctx: OEContext, lines: dict[int, RFQLine]) -> None:
    async def broken(_: str) -> str | None:
        raise ConnectionError("mcp down")

    ctx.lookup_our_part = broken
    line = next(line for line in lines.values() if line.our_part_no is None)
    result = await resolve_line(line, ctx)
    assert isinstance(result, RowFailure) and result.stage == "lookup_our_part"


async def test_chain_cut_off_at_the_hop_limit_is_marked_truncated(
    ctx: OEContext,
    lines: dict[int, RFQLine],
    truth: dict[int, GroundTruthRow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rfq_agent.agents import oe_interchange

    long_chain = _row(
        truth, lambda r: len(r.supersession_chain) == 3 and r.oe_status_expected == "resolved"
    )
    no_chain = _row(
        truth, lambda r: len(r.supersession_chain) == 1 and r.oe_status_expected == "resolved"
    )
    full = await resolve_line(lines[long_chain.row_id], ctx)
    assert isinstance(full, OEResolution) and not full.chain_truncated

    monkeypatch.setattr(oe_interchange, "MAX_HOPS", 1)
    cut = await resolve_line(lines[long_chain.row_id], ctx)
    single = await resolve_line(lines[no_chain.row_id], ctx)
    assert isinstance(cut, OEResolution) and cut.chain_truncated
    assert isinstance(single, OEResolution) and not single.chain_truncated
