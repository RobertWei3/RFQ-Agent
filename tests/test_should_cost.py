"""should_cost tests: pure spec rules, subgraph against generated pages, CLI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from rfq_agent.agents.should_cost import ShouldCostContext, resolve_should_cost
from rfq_agent.agents.should_cost_rules import (
    ExternalField,
    ExternalSpec,
    SpecPage,
    SpecPageFacts,
    reconcile_spec,
    verify_spec_pages,
)
from rfq_agent.cli import app
from rfq_agent.config import Settings
from rfq_agent.data_gen.audit import load_truth
from rfq_agent.data_gen.truth import GroundTruthRow, TruthInternalSpec
from rfq_agent.eval.quick_compare import COST_TOLERANCE, expected_should_cost
from rfq_agent.llm.fake import FakeRequest, register_fake_handler
from rfq_agent.mcp_server import server as internal
from rfq_agent.schemas import (
    MaterialPrice,
    OEResolution,
    PartSpec,
    RFQLine,
    RowFailure,
    ShouldCostResult,
)
from rfq_agent.store.db import connect
from rfq_agent.tools import build_tools
from rfq_agent.tools.base import ToolHit, ToolTimeoutError

KNOWN = ["aluminum", "brass", "cast_iron", "stainless_steel"]
OE = "TK-4471"


def _facts(page_id: str, **kw: Any) -> SpecPageFacts:
    return SpecPageFacts(page_id=page_id, **kw)


def _verify(
    facts: list[SpecPageFacts], pages: dict[str, SpecPage], tolerance: float = 0.05
) -> ExternalSpec:
    return verify_spec_pages(
        facts, pages, current_oe=OE, known_materials=KNOWN, tolerance=tolerance
    )


# --- verify_spec_pages (pure) ---------------------------------------------------------


def test_spec_page_material_and_weight_in_grams() -> None:
    pages = {
        "spec-1": SpecPage(
            kind="spec",
            text=(
                "Product data sheet - Tekma TK-4471\nMaterial: die-cast aluminum\nNet weight: 412 g"
            ),
        )
    }
    facts = [
        _facts(
            "spec-1",
            material_text="die-cast aluminum",
            material_key="aluminum",
            weight_value=412,
            weight_unit="g",
        )
    ]
    ext = _verify(facts, pages)
    assert ext.material.value == "aluminum"
    assert ext.weight_kg.value == pytest.approx(0.412)
    assert ext.category.value is None
    assert [e.ref for e in ext.weight_kg.evidence] == ["spec-1"]
    assert ext.rejected == []


def _spec_page(*lines: str) -> dict[str, SpecPage]:
    return {"spec-1": SpecPage(kind="spec", text="\n".join([f"Data sheet {OE}", *lines]))}


@pytest.mark.parametrize(
    ("line", "value", "unit", "kg"),
    [
        ("Weight 0.908 lb", 0.908, "lb", 0.412),  # 0.908 * 0.45359237 = 0.41186
        ("Shipping weight approx. 2 lbs", 2, "lbs", 0.907),  # 0.90718
        ("Net weight: 1.250 KGS", 1.25, "KGS", 1.25),
        ("Net weight: 0.412kg", 0.412, "kg", 0.412),
    ],
)
def test_weight_units_are_converted_to_kg(line: str, value: float, unit: str, kg: float) -> None:
    ext = _verify([_facts("spec-1", weight_value=value, weight_unit=unit)], _spec_page(line))
    assert ext.weight_kg.value == pytest.approx(kg, rel=1e-4)


@pytest.mark.parametrize(
    ("line", "value", "unit"),
    [
        ("Net weight: 412 g", 0.5, "kg"),  # number not on the page
        ("Net weight: 412 g", 412, "kg"),  # number on the page, but with another unit
        ("Weight: 14 oz", 14, "oz"),  # unit not accepted
        ("Net weight: 0 kg", 0, "kg"),  # must be positive
        ("Model TK-4471 kg rated", 4471, "kg"),  # digits inside the part number
    ],
)
def test_ungrounded_weight_is_rejected(line: str, value: float, unit: str) -> None:
    ext = _verify([_facts("spec-1", weight_value=value, weight_unit=unit)], _spec_page(line))
    assert ext.weight_kg.value is None
    assert [r.page_id for r in ext.rejected] == ["spec-1"]


def test_material_text_must_be_on_the_page() -> None:
    ext = _verify(
        [_facts("spec-1", material_text="cast iron", material_key="cast_iron")],
        _spec_page("Housing material: aluminium"),
    )
    assert ext.material.value is None
    assert [r.page_id for r in ext.rejected] == ["spec-1"]


def test_material_text_match_ignores_case() -> None:
    ext = _verify(
        [_facts("spec-1", material_text="GG25 Iron", material_key="cast_iron")],
        _spec_page("Body: gg25 iron"),
    )
    assert ext.material.value == "cast_iron"
    assert ext.material.evidence[0].snippet == "GG25 Iron"


@pytest.mark.parametrize("key", ["zinc", "other", None])
def test_unknown_material_key_counts_as_other(key: str | None) -> None:
    ext = _verify(
        [_facts("spec-1", material_text="zinc alloy", material_key=key)],
        _spec_page("Material: zinc alloy"),
    )
    assert ext.material.value == "other"


def test_unknown_page_and_page_without_current_oe_are_rejected() -> None:
    pages = {
        "spec-1": SpecPage(kind="spec", text="Data sheet TK-9999\nMaterial: brass\nWeight 0.3 kg"),
    }
    facts = [
        _facts("spec-1", material_text="brass", material_key="brass"),
        _facts("spec-404", material_text="brass", material_key="brass"),
    ]
    ext = _verify(facts, pages)
    assert ext.material.value is None
    assert sorted(r.page_id for r in ext.rejected) == ["spec-1", "spec-404"]


def _catalog_page(line: str) -> dict[str, SpecPage]:
    return {"cat-1": SpecPage(kind="catalog", text=f"OEM catalog {OE}\n{line}")}


@pytest.mark.parametrize(
    ("line", "phrase", "category"),
    [
        ("Description: Engine Thermostat Assy", "Engine Thermostat Assy", "thermostat"),
        ("Description: Coolant Pump", "Coolant Pump", "water_pump"),
        ("Description: water pump", "Water Pump", "water_pump"),
        ("Description: Radiator Hose", "Radiator Hose", None),  # not a category we cost
    ],
)
def test_catalog_category_phrase_is_mapped(line: str, phrase: str, category: str | None) -> None:
    ext = _verify([_facts("cat-1", category_text=phrase)], _catalog_page(line))
    assert ext.category.value == category


def test_catalog_category_must_be_on_the_page_and_spec_fields_come_from_spec_pages() -> None:
    pages = {
        **_catalog_page("Description: Coolant Pump\nMaterial: brass"),
        "spec-1": SpecPage(kind="spec", text=f"Data sheet {OE}\nType: Thermostat"),
    }
    facts = [
        _facts("cat-1", category_text="Thermostat", material_text="brass", material_key="brass"),
        _facts("spec-1", category_text="Thermostat"),
    ]
    ext = _verify(facts, pages)
    assert ext.category.value is None  # catalog phrase not on its page; spec pages give no category
    assert ext.material.value is None  # material only comes from spec pages
    assert [r.page_id for r in ext.rejected] == ["cat-1"]


def _two_spec_pages(first: str, second: str) -> dict[str, SpecPage]:
    return {
        "spec-a": SpecPage(kind="spec", text=f"Data sheet {OE}\n{first}"),
        "spec-b": SpecPage(kind="spec", text=f"{OE} datasheet\n{second}"),
    }


def test_agreeing_pages_merge_value_from_first_page_and_keep_all_evidence() -> None:
    pages = _two_spec_pages("Material: aluminium\nWeight 0.400 kg", "Body: ADC12\nWeight 410 g")
    facts = [
        _facts(
            "spec-b",
            material_text="ADC12",
            material_key="aluminum",
            weight_value=410,
            weight_unit="g",
        ),
        _facts(
            "spec-a",
            material_text="aluminium",
            material_key="aluminum",
            weight_value=0.4,
            weight_unit="kg",
        ),
    ]
    ext = _verify(facts, pages)  # 410 g is +2.5% of 0.400 kg: within 5%
    assert ext.material.value == "aluminum" and not ext.material.disagree
    assert ext.weight_kg.value == pytest.approx(0.4)  # page_id "spec-a" sorts first
    assert [e.ref for e in ext.weight_kg.evidence] == ["spec-a", "spec-b"]


def test_disagreeing_pages_make_the_field_unusable() -> None:
    pages = _two_spec_pages("Material: brass\nWeight 0.400 kg", "Material: SS304\nWeight 0.5 kg")
    facts = [
        _facts(
            "spec-a",
            material_text="brass",
            material_key="brass",
            weight_value=0.4,
            weight_unit="kg",
        ),
        _facts(
            "spec-b",
            material_text="SS304",
            material_key="stainless_steel",
            weight_value=0.5,
            weight_unit="kg",
        ),
    ]
    ext = _verify(facts, pages)  # 0.5 kg is +25% of 0.4 kg
    assert ext.material.value is None and ext.material.disagree
    assert ext.weight_kg.value is None and ext.weight_kg.disagree
    assert [e.ref for e in ext.material.evidence] == ["spec-a", "spec-b"]


def test_disagreeing_catalog_categories_leave_category_unknown() -> None:
    pages = {
        "cat-1": SpecPage(kind="catalog", text=f"{OE}\nDescription: Thermostat"),
        "cat-2": SpecPage(kind="catalog", text=f"{OE}\nDescription: Water Pump"),
    }
    facts = [
        _facts("cat-1", category_text="Thermostat"),
        _facts("cat-2", category_text="Water Pump"),
    ]
    ext = _verify(facts, pages)
    assert ext.category.value is None and ext.category.disagree


# --- reconcile_spec (pure) ------------------------------------------------------------

DISAGREE = "pages disagree"  # marker for an external field whose pages contradict each other


def _external(material: str | None = None, weight: float | str | None = None) -> ExternalSpec:
    def ext(value: Any) -> ExternalField[Any]:
        if value == DISAGREE:
            return ExternalField(value=None, disagree=True)
        return ExternalField(value=value)

    return ExternalSpec(material=ext(material), weight_kg=ext(weight))


@pytest.mark.parametrize(
    ("internal", "external", "used", "fields", "source"),
    [
        # both sides agree (410 g vs 0.400 kg is +2.5%)
        (
            ("brass", 0.4),
            ("brass", 0.41),
            ("brass", 0.4),
            ("both_agree", "both_agree"),
            "both_agree",
        ),
        # internal only
        (("brass", 0.4), (None, None), ("brass", 0.4), ("internal", "internal"), "internal"),
        # external fills a gap; weakest of both_agree and external is external
        (("brass", None), ("brass", 0.3), ("brass", 0.3), ("both_agree", "external"), "external"),
        # weight off by 30%: conflict, internal value wins
        (("brass", 0.4), ("brass", 0.52), ("brass", 0.4), ("both_agree", "conflict"), "conflict"),
        # material differs: conflict beats external as the weakest
        (("brass", None), ("aluminum", 0.3), ("brass", 0.3), ("conflict", "external"), "conflict"),
        # internal only vs external only: external is weaker
        ((None, 0.4), ("brass", None), ("brass", 0.4), ("external", "internal"), "external"),
        # external pages disagree with each other: conflict when internal has a value
        (("brass", 0.4), (DISAGREE, None), ("brass", 0.4), ("conflict", "internal"), "conflict"),
        # ... and missing when it does not
        ((None, 0.4), (DISAGREE, None), (None, 0.4), ("missing", "internal"), "missing"),
        # nothing anywhere for weight: missing overrides a conflict elsewhere
        (("brass", None), ("aluminum", None), ("brass", None), ("conflict", "missing"), "missing"),
    ],
)
def test_reconcile_spec_field_rules_and_weakest_source(
    internal: tuple[str | None, float | None],
    external: tuple[str | None, float | str | None],
    used: tuple[str | None, float | None],
    fields: tuple[str, str],
    source: str,
) -> None:
    r = reconcile_spec(
        internal_material=internal[0],
        internal_weight_kg=internal[1],
        external=_external(*external),
        tolerance=0.05,
    )
    assert (r.material, r.weight_kg) == used
    assert (r.material_source, r.weight_source) == fields
    assert r.spec_source == source


def test_weight_tolerance_is_relative_to_the_internal_value() -> None:
    def source(ext_weight: float) -> str:
        r = reconcile_spec(
            internal_material="brass",
            internal_weight_kg=1.0,
            external=_external("brass", ext_weight),
            tolerance=0.05,
        )
        return r.weight_source

    assert source(1.05) == "both_agree"
    assert source(0.95) == "both_agree"
    assert source(1.06) == "conflict"


# --- expected outcome derived independently from ground truth (quick_compare) ---------


@pytest.fixture(scope="module")
def truth(generated_dataset: Path) -> dict[int, GroundTruthRow]:
    return {t.row_id: t for t in load_truth(generated_dataset)}


def _truth_row(
    truth: dict[int, GroundTruthRow],
    *,
    reach: str = "xref",
    oe: str = "resolved",
    material: bool = True,
    weight: bool = True,
    page: bool = True,
    tags: list[str] | None = None,
) -> GroundTruthRow:
    base = next(iter(truth.values()))
    return base.model_copy(
        update={
            "our_part_reachability": reach,
            "oe_status_expected": oe,
            "internal_spec": TruthInternalSpec(
                material=base.spec.material if material else None,
                weight_kg=base.spec.weight_kg if weight else None,
            ),
            "external_spec_available": page,
            "tags": tags or [],
        }
    )


@pytest.mark.parametrize(
    ("row", "status", "source", "part_cycle"),
    [
        ({}, "estimated", "both_agree", True),
        ({"page": False}, "estimated", "internal", True),
        ({"oe": "not_found", "page": False}, "estimated", "internal", True),
        ({"oe": "multiple", "weight": False}, "insufficient_data", "missing", True),
        ({"weight": False}, "estimated", "external", True),
        ({"tags": ["spec_conflict"]}, "estimated", "conflict", True),
        # the corrupted page is never read when the OE is not unique
        ({"tags": ["spec_conflict"], "oe": "multiple"}, "estimated", "internal", True),
        ({"reach": "unreachable"}, "estimated", "external", False),
        ({"reach": "unreachable", "page": False}, "insufficient_data", "missing", False),
        ({"reach": "unreachable", "oe": "multiple"}, "insufficient_data", "missing", False),
    ],
)
def test_expected_should_cost_follows_the_p5_rules(
    truth: dict[int, GroundTruthRow],
    row: dict[str, Any],
    status: str,
    source: str,
    part_cycle: bool,
) -> None:
    t = _truth_row(truth, **row)
    want = expected_should_cost(t)
    assert (want.status, want.spec_source, want.part_cycle_time) == (status, source, part_cycle)
    if status == "estimated":  # internal data and uncorrupted pages both hold the true spec
        assert (want.material, want.weight_kg) == (t.spec.material, t.spec.weight_kg)


# --- subgraph on generated pages ------------------------------------------------------


class DirectInternalDB:
    """In-process internal DB: the MCP server's tool functions without the stdio hop."""

    async def get_part_spec(self, our_part_no: str) -> PartSpec:
        return internal.get_part_spec(our_part_no)

    async def list_materials(self) -> list[str]:
        return internal.list_materials().materials

    async def get_material_price(self, material: str) -> MaterialPrice:
        return internal.get_material_price(material)


@pytest.fixture
def ctx(
    settings: Settings, generated_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> ShouldCostContext:
    monkeypatch.setenv(internal.ENV_INTERNAL_DB, str(generated_dataset / "internal.db"))
    return ShouldCostContext(
        tools=build_tools(generated_dataset, settings.tools),
        settings=settings,
        internal_db=DirectInternalDB(),
    )


def _line(t: GroundTruthRow, description: str | None = None) -> RFQLine:
    return RFQLine(
        row_id=t.row_id,
        customer_part_no_raw=t.customer_part_no_raw,
        customer_part_no=t.customer_part_no,
        our_part_no=t.our_part_no if t.our_part_reachability == "given" else None,
        description=description,
        annual_qty=None,
        customer_name=None,
    )


def _oe(t: GroundTruthRow) -> OEResolution:
    """Upstream input built from ground truth so these tests isolate should_cost."""
    status = t.oe_status_expected
    known = t.our_part_reachability != "unreachable"
    return OEResolution(
        row_id=t.row_id,
        our_part_no=t.our_part_no if known else None,
        our_part_source=t.our_part_reachability if known else "none",  # type: ignore[arg-type]
        status=status,
        current_oe=t.primary_oe if status == "resolved" else None,
        supersession_chain=t.supersession_chain if status == "resolved" else [],
        candidates=[],
        reasoning="from truth",
    )


async def test_all_rows_match_truth(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    results = await asyncio.gather(
        *(resolve_should_cost(_line(t), _oe(t), ctx) for t in truth.values())
    )
    for result in results:
        assert isinstance(result, ShouldCostResult), result
        t, want = truth[result.row_id], expected_should_cost(truth[result.row_id])
        got = (result.status, result.spec_source, result.category, result.material)
        assert got == (want.status, want.spec_source, want.category, want.material), (
            result.row_id,
            result.reasoning,
        )
        if want.weight_kg is None:
            assert result.weight_kg is None
        else:
            assert result.weight_kg == pytest.approx(want.weight_kg, rel=0.01), result.row_id
        if want.status != "estimated":
            assert result.breakdown is None
            continue
        assert result.breakdown is not None
        if want.part_cycle_time:
            assert result.breakdown.landed_cost == pytest.approx(
                t.cost.landed_cost, rel=COST_TOLERANCE
            ), result.row_id
        else:
            assert "category default cycle time" in result.reasoning


class _CountingTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def search(self, query: str) -> list[ToolHit]:
        self.calls += 1
        return []


def _llm_calls(settings: Settings, run_id: str) -> int:
    with connect(settings.db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE rfq_run_id = ?", (run_id,)
        ).fetchone()
    conn.close()
    return int(count)


def _complete_internal(truth: dict[int, GroundTruthRow], oe: str) -> GroundTruthRow:
    return next(
        t
        for t in truth.values()
        if t.oe_status_expected == oe
        and t.our_part_reachability != "unreachable"
        and t.internal_spec.material is not None
        and t.internal_spec.weight_kg is not None
    )


async def test_upstream_failure_and_non_unique_oe_call_no_tool_or_llm(
    ctx: ShouldCostContext, settings: Settings, truth: dict[int, GroundTruthRow]
) -> None:
    tools = {name: _CountingTool(name) for name in ("external_spec_search", "oem_catalog_search")}
    ctx = ShouldCostContext(
        tools=tools, settings=settings, internal_db=ctx.internal_db, run_id="sc-short-circuit"
    )
    t = _complete_internal(truth, "multiple")

    failed = await resolve_should_cost(
        _line(t), RowFailure(row_id=t.row_id, stage="search", error="boom"), ctx
    )
    assert isinstance(failed, RowFailure) and failed.stage == "upstream_oe"

    result = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(result, ShouldCostResult)
    assert (result.status, result.spec_source) == ("estimated", "internal")
    assert "OE not uniquely determined" in result.reasoning

    assert sum(tool.calls for tool in tools.values()) == 0
    assert _llm_calls(settings, "sc-short-circuit") == 0


class _TimeoutTool:
    def __init__(self, name: str) -> None:
        self.name = name

    async def search(self, query: str) -> list[ToolHit]:
        raise ToolTimeoutError(f"{self.name} timed out")


def _missing_weight(truth: dict[int, GroundTruthRow]) -> GroundTruthRow:
    return next(
        t
        for t in truth.values()
        if t.oe_status_expected == "resolved"
        and t.our_part_reachability != "unreachable"
        and t.internal_spec.weight_kg is None
    )


def _with_tools(ctx: ShouldCostContext, **tools: Any) -> ShouldCostContext:
    return ShouldCostContext(
        tools={**ctx.tools, **tools}, settings=ctx.settings, internal_db=ctx.internal_db
    )


async def test_search_failure_only_sinks_the_row_when_pages_fill_gaps(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    ctx = _with_tools(ctx, external_spec_search=_TimeoutTool("external_spec_search"))

    complete = _complete_internal(truth, "resolved")
    result = await resolve_should_cost(_line(complete), _oe(complete), ctx)
    assert isinstance(result, ShouldCostResult)
    assert (result.status, result.spec_source) == ("estimated", "internal")
    assert "cross-check failed at search_spec" in result.reasoning

    gap = _missing_weight(truth)
    failed = await resolve_should_cost(_line(gap), _oe(gap), ctx)
    assert isinstance(failed, RowFailure) and failed.stage == "search_spec"


@pytest.fixture
def broken_spec_extraction() -> Iterator[None]:
    from rfq_agent.agents import fake_handlers

    @register_fake_handler("SpecExtraction")
    def handler(request: FakeRequest) -> dict[str, Any]:
        return {"pages": "nope"}

    yield
    register_fake_handler("SpecExtraction")(fake_handlers.fake_spec)


@pytest.mark.usefixtures("broken_spec_extraction")
async def test_extraction_failure_only_sinks_the_row_when_pages_fill_gaps(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    complete = _complete_internal(truth, "resolved")
    result = await resolve_should_cost(_line(complete), _oe(complete), ctx)
    assert isinstance(result, ShouldCostResult) and result.spec_source == "internal"
    assert "cross-check failed at extract_spec" in result.reasoning

    gap = _missing_weight(truth)
    failed = await resolve_should_cost(_line(gap), _oe(gap), ctx)
    assert isinstance(failed, RowFailure) and failed.stage == "extract_spec"


class _BrokenInternalDB(DirectInternalDB):
    async def get_part_spec(self, our_part_no: str) -> PartSpec:
        raise FileNotFoundError("internal.db")


async def test_internal_db_error_is_row_failure(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    ctx = ShouldCostContext(tools=ctx.tools, settings=ctx.settings, internal_db=_BrokenInternalDB())
    t = _complete_internal(truth, "resolved")
    failed = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(failed, RowFailure) and failed.stage == "internal_spec"


async def test_unknown_part_falls_back_to_rfq_description_for_category(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    ctx = _with_tools(ctx, oem_catalog_search=_CountingTool("oem_catalog_search"))
    t = next(
        t
        for t in truth.values()
        if t.our_part_reachability == "unreachable"
        and t.oe_status_expected == "resolved"
        and t.external_spec_available
        and t.spec.category == "water_pump"
    )
    result = await resolve_should_cost(_line(t, description="WATER PUMP W/ GASKET"), _oe(t), ctx)
    assert isinstance(result, ShouldCostResult)
    assert (result.status, result.category) == ("estimated", "water_pump")
    assert "category water_pump from the RFQ description" in result.reasoning

    unknown = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(unknown, ShouldCostResult)
    assert (unknown.status, unknown.category, unknown.breakdown) == (
        "insufficient_data",
        None,
        None,
    )
    assert unknown.spec_source == "external"  # the spec itself is complete
    assert "category unknown" in unknown.reasoning


async def test_conflict_row_explains_and_keeps_both_sources_as_evidence(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    t = next(
        t
        for t in truth.values()
        if "spec_conflict" in t.tags and t.oe_status_expected == "resolved"
    )
    result = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(result, ShouldCostResult) and result.spec_source == "conflict"
    assert "conflict: internal" in result.reasoning and "internal used" in result.reasoning
    assert (result.material, result.weight_kg) == (t.spec.material, t.spec.weight_kg)
    sources = {e.source for e in result.evidence}
    assert sources == {"internal_db", "external_spec"}
    assert result.cycle_time_min == t.spec.cycle_time_min


# --- comparison table (quick_compare) ---------------------------------------------------


def _result_from_truth(t: GroundTruthRow) -> ShouldCostResult:
    want = expected_should_cost(t)
    estimated = want.status == "estimated"
    return ShouldCostResult(
        row_id=t.row_id,
        category=want.category,
        material=want.material,
        weight_kg=want.weight_kg,
        spec_source=want.spec_source,  # type: ignore[arg-type]
        breakdown=t.cost.model_copy() if estimated else None,
        status=want.status,  # type: ignore[arg-type]
        reasoning="",
    )


def test_compare_should_cost_checks_each_item(truth: dict[int, GroundTruthRow]) -> None:
    from rfq_agent.eval.quick_compare import compare_should_cost, render_should_cost_markdown

    def first(**row: Any) -> GroundTruthRow:
        return next(
            t
            for t in truth.values()
            if all(getattr(t, k) == v for k, v in row.items())
            and expected_should_cost(t).status == "estimated"
        )

    t = first(our_part_reachability="xref", oe_status_expected="resolved")
    unknown = first(our_part_reachability="unreachable", oe_status_expected="resolved")
    insufficient = next(
        t for t in truth.values() if expected_should_cost(t).status == "insufficient_data"
    )

    good = _result_from_truth(t)
    wrong = _result_from_truth(t)
    assert wrong.weight_kg is not None and wrong.breakdown is not None
    wrong.weight_kg *= 1.02  # 2% > 1% tolerance
    wrong.spec_source = "conflict"
    wrong.breakdown.landed_cost *= 1.02
    default_cycle = _result_from_truth(unknown)
    assert default_cycle.breakdown is not None
    default_cycle.breakdown.landed_cost *= 1.10  # category default cycle time: error reported only
    gap = _result_from_truth(insufficient)
    failure = RowFailure(row_id=t.row_id, stage="search_spec", error="boom")

    [ok] = compare_should_cost([good], truth.values())
    assert (ok.status_ok, ok.spec_ok, ok.spec_source_ok, ok.cost_ok, ok.correct) == (
        True,
        True,
        True,
        True,
        True,
    )
    [bad] = compare_should_cost([wrong], truth.values())
    assert (bad.spec_ok, bad.spec_source_ok, bad.cost_ok, bad.correct) == (
        False,
        False,
        False,
        False,
    )
    assert bad.landed_error == pytest.approx(0.02)
    [default] = compare_should_cost([default_cycle], truth.values())
    assert default.cost_ok is None and default.correct
    assert default.landed_error == pytest.approx(0.10)
    [short] = compare_should_cost([gap], truth.values())
    assert short.correct and short.cost_ok is None
    [fail] = compare_should_cost([failure], truth.values())
    assert fail.failed and not fail.correct

    report = render_should_cost_markdown([ok, bad, default, short, fail])
    assert report.splitlines()[-1] == (
        "**Summary:** should-cost correct 3/5 · spec correct 3/5 · spec_source correct 3/5 · "
        "cost correct 1/3 (part cycle time) · landed MAPE 1.0% (part cycle time, n=2) / "
        "10.0% (category default, n=1) · insufficient_data 1 · failed rows 1"
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
    out = tmp_path / "cost.jsonl"
    sample = str(generated_dataset / "rfq_sample_20.xlsx")
    result = CliRunner().invoke(
        app, ["agent", "should-cost", "--input", sample, "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    records = _records(out)
    assert [r["row_id"] for r in records] == list(range(1, 21))
    assert all(r["ok"] for r in records)
    ShouldCostResult.model_validate(records[0]["result"])
    assert out.with_suffix(".oe.jsonl").is_file()
    assert "should-cost correct 20/20" in out.with_suffix(".compare.md").read_text()
    trace = _records(out.with_suffix(".trace.jsonl"))
    assert {e["agent"] for e in trace} == {"oe_interchange", "should_cost"}
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["agent"] == "should_cost" and meta["input"].endswith("rfq_sample_20.xlsx")


@pytest.mark.usefixtures("cli_config")
def test_cli_from_oe_rereads_the_rfq_recorded_upstream(
    generated_dataset: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    oe_out = tmp_path / "oe.jsonl"
    sample = str(generated_dataset / "rfq_sample_20.xlsx")
    first = runner.invoke(
        app, ["agent", "oe-interchange", "--input", sample, "--output", str(oe_out)]
    )
    assert first.exit_code == 0, first.output
    out = tmp_path / "cost.jsonl"
    result = runner.invoke(
        app, ["agent", "should-cost", "--from-oe", str(oe_out), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "should-cost correct 20/20" in out.with_suffix(".compare.md").read_text()
    trace = _records(out.with_suffix(".trace.jsonl"))
    assert {e["agent"] for e in trace} == {"should_cost"}

    oe_out.with_suffix(".meta.json").unlink()  # upstream input no longer known
    missing = runner.invoke(app, ["agent", "should-cost", "--from-oe", str(oe_out)])
    assert missing.exit_code == 2
    assert "--rfq" in missing.output

    explicit = tmp_path / "explicit.jsonl"
    given = runner.invoke(
        app,
        ["agent", "should-cost", "--from-oe", str(oe_out), "--rfq", sample]
        + ["--truth", str(generated_dataset / "rfq_sample_20_ground_truth.jsonl")]
        + ["--output", str(explicit)],
    )
    assert given.exit_code == 0, given.output
    assert "should-cost correct 20/20" in explicit.with_suffix(".compare.md").read_text()
    meta = json.loads(explicit.with_suffix(".meta.json").read_text())
    assert meta["rfq"].endswith("rfq_sample_20.xlsx")


@pytest.mark.usefixtures("cli_config")
def test_cli_rfq_option_only_goes_with_from_oe(generated_dataset: Path) -> None:
    sample = str(generated_dataset / "rfq_sample_20.xlsx")
    result = CliRunner().invoke(app, ["agent", "should-cost", "--input", sample, "--rfq", sample])
    assert result.exit_code == 2
    assert "--rfq" in result.output


@pytest.mark.usefixtures("cli_config")
def test_cli_refuses_real_provider_without_flag(
    generated_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RFQ_LLM_PROVIDER")
    result = CliRunner().invoke(
        app, ["agent", "should-cost", "--input", str(generated_dataset / "rfq_sample_20.xlsx")]
    )
    assert result.exit_code == 2
    assert "--allow-real" in result.output


def test_compare_does_not_judge_a_category_taken_from_the_rfq_description(
    truth: dict[int, GroundTruthRow],
) -> None:
    from rfq_agent.eval.quick_compare import compare_should_cost

    # Ground truth has no RFQ description, so it cannot expect a category for an unknown
    # part without a unique OE; the agent may still name one from the description.
    t = next(
        t
        for t in truth.values()
        if t.our_part_reachability == "unreachable" and t.oe_status_expected == "multiple"
    )
    result = _result_from_truth(t)
    assert result.category is None
    result.category = t.spec.category
    [check] = compare_should_cost([result], truth.values())
    assert check.spec_ok and check.correct


# --- review fixes -----------------------------------------------------------------------


def test_weight_unit_must_be_on_the_same_line() -> None:
    ext = _verify(
        [_facts("spec-1", weight_value=0.4, weight_unit="kg")], _spec_page("Rev 0.4", "kg")
    )
    assert ext.weight_kg.value is None


def test_weight_with_thousands_separator_is_grounded() -> None:
    ext = _verify(
        [_facts("spec-1", weight_value=1200, weight_unit="g")], _spec_page("Net weight: 1,200 g")
    )
    assert ext.weight_kg.value == pytest.approx(1.2)


class _PriceDB(DirectInternalDB):
    def __init__(self, price: MaterialPrice | Exception) -> None:
        self._price = price

    async def get_material_price(self, material: str) -> MaterialPrice:
        if isinstance(self._price, Exception):
            raise self._price
        return self._price


@pytest.mark.parametrize(
    ("price", "reason"),
    [
        (MaterialPrice(found=False, material="x"), "no internal material price"),
        (
            MaterialPrice(found=True, material="x", price_per_kg=3.0, currency="EUR"),
            "in EUR, not USD",
        ),
    ],
)
async def test_unusable_material_price_is_insufficient_data_with_spec_source_kept(
    ctx: ShouldCostContext,
    truth: dict[int, GroundTruthRow],
    price: MaterialPrice,
    reason: str,
) -> None:
    ctx = ShouldCostContext(tools=ctx.tools, settings=ctx.settings, internal_db=_PriceDB(price))
    t = next(t for t in truth.values() if expected_should_cost(t).spec_source == "both_agree")
    result = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(result, ShouldCostResult)
    assert (result.status, result.breakdown, result.spec_source) == (
        "insufficient_data",
        None,
        "both_agree",
    )
    assert reason in result.reasoning


async def test_material_price_lookup_error_is_its_own_stage(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    ctx = ShouldCostContext(
        tools=ctx.tools, settings=ctx.settings, internal_db=_PriceDB(RuntimeError("mcp down"))
    )
    t = _complete_internal(truth, "resolved")
    failed = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(failed, RowFailure) and failed.stage == "material_price"


async def test_insufficient_row_does_not_claim_a_cycle_time_was_used(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    t = next(
        t
        for t in truth.values()
        if t.our_part_reachability == "unreachable"
        and t.oe_status_expected == "resolved"
        and not t.external_spec_available
    )
    result = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(result, ShouldCostResult) and result.status == "insufficient_data"
    assert "cycle time" not in result.reasoning


async def test_given_part_missing_from_internal_db_is_costed_as_unknown_part(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    t = next(
        t
        for t in truth.values()
        if t.oe_status_expected == "resolved"
        and t.external_spec_available
        and t.our_part_reachability != "unreachable"
    )
    line = _line(t).model_copy(update={"our_part_no": "THM-99999"})
    upstream = _oe(t).model_copy(update={"our_part_no": "THM-99999", "our_part_source": "given"})
    result = await resolve_should_cost(line, upstream, ctx)
    assert isinstance(result, ShouldCostResult)
    assert (result.status, result.spec_source, result.category) == (
        "estimated",
        "external",
        t.spec.category,
    )
    assert "our part THM-99999 not in the internal DB" in result.reasoning
    assert "category default cycle time" in result.reasoning


@pytest.mark.parametrize(
    ("line", "value", "unit", "kg"),
    [
        ("Net weight: 412 g", 412, "g", 0.412),  # 412 * 0.001 is 0.41200000000000003 in floats
        ("Weight 0.908 lb", 0.908, "lb", 0.412),  # 0.41186...
        ("Weight 4.149 lb", 4.149, "lb", 1.882),  # 1.88195...
        ("Weight 2.5 g", 2.5, "g", 0.003),  # 0.0025 rounds half up
    ],
)
def test_converted_weight_is_rounded_half_up_to_grams(
    line: str, value: float, unit: str, kg: float
) -> None:
    ext = _verify([_facts("spec-1", weight_value=value, weight_unit=unit)], _spec_page(line))
    assert ext.weight_kg.value == kg


class _NoCategoryDB(DirectInternalDB):
    async def get_part_spec(self, our_part_no: str) -> PartSpec:
        spec = await super().get_part_spec(our_part_no)
        return spec.model_copy(update={"category": None})


async def test_missing_internal_category_is_filled_from_the_catalog_page(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    ctx = ShouldCostContext(tools=ctx.tools, settings=ctx.settings, internal_db=_NoCategoryDB())
    t = _complete_internal(truth, "resolved")
    result = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(result, ShouldCostResult)
    assert (result.status, result.category) == ("estimated", t.spec.category)
    assert result.cycle_time_min == t.spec.cycle_time_min  # the part's own cycle time
    assert "category missing internally" in result.reasoning
    assert "from the OEM catalog page" in result.reasoning
    assert result.breakdown is not None
    assert result.breakdown.landed_cost == pytest.approx(t.cost.landed_cost, rel=COST_TOLERANCE)


async def test_material_price_without_currency_is_not_assumed_usd(
    ctx: ShouldCostContext, truth: dict[int, GroundTruthRow]
) -> None:
    price = MaterialPrice(found=True, material="x", price_per_kg=3.0, currency=None)
    ctx = ShouldCostContext(tools=ctx.tools, settings=ctx.settings, internal_db=_PriceDB(price))
    t = _complete_internal(truth, "resolved")
    result = await resolve_should_cost(_line(t), _oe(t), ctx)
    assert isinstance(result, ShouldCostResult) and result.status == "insufficient_data"
    assert "not USD" in result.reasoning


def test_disagreeing_pages_without_internal_value_are_not_called_unavailable() -> None:
    from rfq_agent.agents.should_cost import spec_notes

    external = _external(material=DISAGREE, weight=0.4)
    spec = reconcile_spec(
        internal_material=None, internal_weight_kg=0.4, external=external, tolerance=0.05
    )
    notes = spec_notes(None, external, spec)
    assert "external pages disagree on material; needs manual review" in notes
    assert not any("material not available" in n for n in notes)
