from __future__ import annotations

from pathlib import Path

import pytest

from rfq_agent.config import FaultSettings, ToolSettings
from rfq_agent.data_gen.audit import load_truth
from rfq_agent.data_gen.world import variant_spelling
from rfq_agent.tools import TOOL_CORPORA, ToolProvider, ToolTimeoutError, build_tools
from rfq_agent.tools.corpus import PageCorpusTool


@pytest.fixture(scope="module")
def tools(generated_dataset: Path) -> dict[str, PageCorpusTool]:
    return build_tools(generated_dataset, ToolSettings())


def test_all_section9_tools_exist(tools: dict[str, PageCorpusTool]) -> None:
    assert (
        set(tools)
        == {
            "oem_catalog_search",
            "interchange_search",
            "msrp_lookup",
            "retail_search_1",
            "retail_search_2",
            "retail_search_3",
            "retail_search_4",
            "retail_search_5",
            "external_spec_search",
        }
        == set(TOOL_CORPORA)
    )
    assert all(isinstance(t, ToolProvider) for t in tools.values())


async def test_exact_hit_first_with_decoys(
    tools: dict[str, PageCorpusTool], generated_dataset: Path
) -> None:
    truth = load_truth(generated_dataset)
    row = next(t for t in truth if t.primary_oe and len(t.accepted_oes) == 1)
    hits = await tools["oem_catalog_search"].search(row.primary_oe)  # type: ignore[arg-type]
    assert row.primary_oe in hits[0].text
    assert 1 <= len(hits) <= 5
    assert all(row.primary_oe not in h.text for h in hits[1:])  # decoys are other parts


async def test_variant_spelling_query_matches(
    tools: dict[str, PageCorpusTool], generated_dataset: Path
) -> None:
    import random

    truth = load_truth(generated_dataset)
    row = next(t for t in truth if t.primary_oe)
    query = variant_spelling(row.primary_oe, random.Random(1))  # type: ignore[arg-type]
    hits = await tools["msrp_lookup"].search(f"  {query} ")
    assert any(row.primary_oe in h.text for h in hits)


async def test_interchange_by_dirty_customer_number(
    tools: dict[str, PageCorpusTool], generated_dataset: Path
) -> None:
    truth = load_truth(generated_dataset)
    row = next(t for t in truth if "dirty_input" in t.tags and t.primary_oe)
    hits = await tools["interchange_search"].search(row.customer_part_no_raw)
    assert row.customer_part_no in hits[0].text


async def test_unknown_and_empty_queries(tools: dict[str, PageCorpusTool]) -> None:
    assert await tools["msrp_lookup"].search("   ") == []
    hits = await tools["msrp_lookup"].search("ZZZZ-NOT-A-PART")
    assert all("ZZZZ" not in h.text for h in hits)


async def test_fault_injection_is_deterministic(generated_dataset: Path) -> None:
    def build(**kw: float) -> PageCorpusTool:
        faults = FaultSettings(enabled=True, seed=3, **kw)
        return build_tools(generated_dataset, ToolSettings(faults=faults))["msrp_lookup"]

    with pytest.raises(ToolTimeoutError):
        await build(timeout_rate=1.0).search("anything")
    assert await build(empty_rate=1.0).search("0B7-379-712J") == []

    tool = build(timeout_rate=0.3, empty_rate=0.3)
    outcomes = []
    for i in range(60):
        try:
            outcomes.append(len(await tool.search(f"Q{i:04d}")))
        except ToolTimeoutError:
            outcomes.append(-1)
    again = []
    for i in range(60):
        try:
            again.append(len(await tool.search(f"Q{i:04d}")))
        except ToolTimeoutError:
            again.append(-1)
    assert outcomes == again
    assert -1 in outcomes


def test_missing_dataset_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="rfq data generate"):
        build_tools(tmp_path, ToolSettings())
