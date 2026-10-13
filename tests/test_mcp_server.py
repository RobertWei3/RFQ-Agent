from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from rfq_agent.data_gen.audit import load_truth
from rfq_agent.mcp_server.server import ENV_INTERNAL_DB, mcp

TOOLS = {
    "lookup_our_part",
    "get_part_spec",
    "get_material_price",
    "list_materials",
    "get_cost_params",
}


@pytest.fixture
def internal_db(generated_dataset: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = generated_dataset / "internal.db"
    monkeypatch.setenv(ENV_INTERNAL_DB, str(path))
    return path


@pytest.mark.usefixtures("internal_db")
async def test_all_tools_in_memory(generated_dataset: Path) -> None:
    truth = load_truth(generated_dataset)
    xref_row = next(t for t in truth if t.our_part_reachability == "xref")
    unreachable = next(t for t in truth if t.our_part_reachability == "unreachable")
    missing = next(t for t in truth if "internal_spec_missing" in t.tags)

    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == TOOLS

        async def call(name: str, args: dict[str, object]) -> dict[str, object]:
            result = await client.call_tool(name, args)
            assert not result.isError, result.content
            assert result.structuredContent is not None
            return result.structuredContent

        hit = await call(
            "lookup_our_part", {"customer_part_no": f" {xref_row.customer_part_no.lower()} "}
        )
        assert hit == {
            "found": True,
            "customer_part_no": f" {xref_row.customer_part_no.lower()} ",
            "our_part_no": xref_row.our_part_no,
        }
        miss = await call("lookup_our_part", {"customer_part_no": unreachable.customer_part_no})
        assert miss["found"] is False

        spec = await call("get_part_spec", {"our_part_no": xref_row.our_part_no})
        assert spec["found"] and spec["category"] == xref_row.spec.category
        assert spec["cycle_time_min"] == xref_row.spec.cycle_time_min
        partial = await call("get_part_spec", {"our_part_no": missing.our_part_no})
        assert partial["material"] is None or partial["weight_kg"] is None
        assert (await call("get_part_spec", {"our_part_no": "NOPE-1"}))["found"] is False

        price = await call("get_material_price", {"material": "Cast Iron"})
        assert price == {
            "schema_version": "1.0",
            "found": True,
            "material": "cast_iron",
            "price_per_kg": 1.2,
            "currency": "USD",
            "as_of": "2026-01-01",
        }
        assert (await call("get_material_price", {"material": "unobtainium"}))["found"] is False

        materials = await call("list_materials", {})
        assert materials == {
            "schema_version": "1.0",
            "materials": ["aluminum", "brass", "cast_iron", "stainless_steel"],
        }

        params = await call("get_cost_params", {})
        assert params["units_per_shipment"] == 2000
        assert set(params["categories"]) == {"thermostat", "water_pump"}  # type: ignore[arg-type]


async def test_missing_db_reports_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_INTERNAL_DB, str(tmp_path / "absent.db"))
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        result = await client.call_tool("get_part_spec", {"our_part_no": "X"})
        assert result.isError


async def test_stdio_server_via_cli(internal_db: Path, config_dir: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rfq_agent.cli", "mcp", "serve"],
        env={
            ENV_INTERNAL_DB: str(internal_db),
            "RFQ_CONFIG_DIR": str(config_dir),
            "RFQ_LLM_PROVIDER": "fake",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == TOOLS
        result = await session.call_tool("get_material_price", {"material": "brass"})
        assert result.structuredContent == {
            "schema_version": "1.0",
            "found": True,
            "material": "brass",
            "price_per_kg": 8.0,
            "currency": "USD",
            "as_of": "2026-01-01",
        }
