"""Run agents over rows (one shared MCP session for oe_interchange) and read/write JSONL."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import BaseModel

from rfq_agent.agents.common import RowFailure
from rfq_agent.agents.oe_interchange import OEContext, resolve_line
from rfq_agent.agents.oe_msrp import MSRPContext, MSRPInput, resolve_msrp
from rfq_agent.agents.retail_price import RetailContext, RetailInput, resolve_retail
from rfq_agent.agents.should_cost import (
    InternalDB,
    ShouldCostContext,
    ShouldCostInput,
    resolve_should_cost,
)
from rfq_agent.config import ENV_CONFIG_DIR, ENV_FORCE_PROVIDER, ENV_SKIP_DOTENV, Settings
from rfq_agent.mcp_server.server import ENV_INTERNAL_DB
from rfq_agent.schemas import (
    MaterialList,
    MaterialPrice,
    MSRPResult,
    OEResolution,
    PartSpec,
    RetailResult,
    RFQLine,
    ShouldCostResult,
)
from rfq_agent.tools import build_tools
from rfq_agent.trace import TraceWriter

Outcome = OEResolution | MSRPResult | RetailResult | ShouldCostResult | RowFailure
Lookup = Callable[[str], Awaitable[str | None]]


McpCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@asynccontextmanager
async def mcp_internal_session(settings: Settings, dataset_dir: Path) -> AsyncIterator[McpCall]:
    """Start the internal-DB MCP server (stdio) once; yield ``call(tool, args) -> dict``."""
    env = {
        ENV_INTERNAL_DB: str(dataset_dir / "internal.db"),
        ENV_CONFIG_DIR: str(settings.config_dir),
        "PATH": os.environ.get("PATH", ""),
    }
    for name in (ENV_FORCE_PROVIDER, ENV_SKIP_DOTENV):
        if os.environ.get(name):
            env[name] = os.environ[name]
    client = MultiServerMCPClient(
        {
            "internal": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "rfq_agent.cli", "mcp", "serve"],
                "env": env,
            }
        }
    )
    async with client.session("internal") as session:
        tools = {tool.name: tool for tool in await load_mcp_tools(session)}

        async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
            message = await tools[name].ainvoke(
                {"type": "tool_call", "id": uuid.uuid4().hex, "name": name, "args": args}
            )
            artifact = getattr(message, "artifact", None) or {}
            data: dict[str, Any] = artifact.get("structured_content") or {}
            if not data:
                raise RuntimeError(f"{name} returned no structured content: {message}")
            return data

        yield call


@asynccontextmanager
async def mcp_our_part_lookup(settings: Settings, dataset_dir: Path) -> AsyncIterator[Lookup]:
    """``lookup_our_part`` over one MCP session."""
    async with mcp_internal_session(settings, dataset_dir) as call:

        async def lookup(customer_part_no: str) -> str | None:
            data = await call("lookup_our_part", {"customer_part_no": customer_part_no})
            return data["our_part_no"] if data.get("found") else None

        yield lookup


class McpInternalDB:
    """``should_cost``'s internal-DB calls over one MCP session."""

    def __init__(self, call: McpCall) -> None:
        self._call = call

    async def get_part_spec(self, our_part_no: str) -> PartSpec:
        return PartSpec.model_validate(
            await self._call("get_part_spec", {"our_part_no": our_part_no})
        )

    async def list_materials(self) -> list[str]:
        return MaterialList.model_validate(await self._call("list_materials", {})).materials

    async def get_material_price(self, material: str) -> MaterialPrice:
        return MaterialPrice.model_validate(
            await self._call("get_material_price", {"material": material})
        )


async def run_oe_interchange(
    lines: Sequence[RFQLine],
    settings: Settings,
    *,
    dataset_dir: Path,
    run_id: str,
    lookup: Lookup | None = None,
    model: BaseChatModel | None = None,
    trace: TraceWriter | None = None,
) -> list[OEResolution | RowFailure]:
    tools = build_tools(dataset_dir, settings.tools)

    async def run_all(lookup_fn: Lookup) -> list[OEResolution | RowFailure]:
        ctx = OEContext(
            tools=tools,
            lookup_our_part=lookup_fn,
            settings=settings,
            run_id=run_id,
            model=model,
            trace=trace,
        )
        return list(await asyncio.gather(*(resolve_line(line, ctx) for line in lines)))

    if lookup is not None:
        return await run_all(lookup)
    async with mcp_our_part_lookup(settings, dataset_dir) as mcp_lookup:
        return await run_all(mcp_lookup)


async def run_oe_msrp(
    inputs: Sequence[MSRPInput],
    settings: Settings,
    *,
    dataset_dir: Path,
    run_id: str,
    model: BaseChatModel | None = None,
    trace: TraceWriter | None = None,
) -> list[MSRPResult | RowFailure]:
    ctx = MSRPContext(
        tools=build_tools(dataset_dir, settings.tools),
        settings=settings,
        run_id=run_id,
        model=model,
        trace=trace,
    )
    return list(await asyncio.gather(*(resolve_msrp(item, ctx) for item in inputs)))


async def run_retail_price(
    inputs: Sequence[RetailInput],
    settings: Settings,
    *,
    dataset_dir: Path,
    run_id: str,
    model: BaseChatModel | None = None,
    trace: TraceWriter | None = None,
) -> list[RetailResult | RowFailure]:
    ctx = RetailContext(
        tools=build_tools(dataset_dir, settings.tools),
        settings=settings,
        run_id=run_id,
        model=model,
        trace=trace,
    )
    return list(await asyncio.gather(*(resolve_retail(item, ctx) for item in inputs)))


async def run_should_cost(
    inputs: Sequence[ShouldCostInput],
    settings: Settings,
    *,
    dataset_dir: Path,
    run_id: str,
    internal_db: InternalDB | None = None,
    model: BaseChatModel | None = None,
    trace: TraceWriter | None = None,
) -> list[ShouldCostResult | RowFailure]:
    tools = build_tools(dataset_dir, settings.tools)

    async def run_all(db: InternalDB) -> list[ShouldCostResult | RowFailure]:
        ctx = ShouldCostContext(
            tools=tools,
            internal_db=db,
            settings=settings,
            run_id=run_id,
            model=model,
            trace=trace,
        )
        return list(
            await asyncio.gather(
                *(resolve_should_cost(item.line, item.upstream, ctx) for item in inputs)
            )
        )

    if internal_db is not None:
        return await run_all(internal_db)
    async with mcp_internal_session(settings, dataset_dir) as call:
        return await run_all(McpInternalDB(call))


def outcome_record(outcome: Outcome) -> dict[str, Any]:
    if isinstance(outcome, RowFailure):
        return {
            "row_id": outcome.row_id,
            "ok": False,
            "stage": outcome.stage,
            "error": outcome.error,
        }
    return {"row_id": outcome.row_id, "ok": True, "result": outcome.model_dump(mode="json")}


def write_outcomes(outcomes: Sequence[Outcome], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for outcome in sorted(outcomes, key=lambda o: o.row_id):
            fh.write(json.dumps(outcome_record(outcome), ensure_ascii=False) + "\n")


R = TypeVar("R", bound=BaseModel)


def read_outcomes(path: Path, result_type: type[R]) -> list[R | RowFailure]:
    """Read an agent JSONL (success and failure records)."""
    outcomes: list[R | RowFailure] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            if record["ok"]:
                outcomes.append(result_type.model_validate(record["result"]))
            else:
                outcomes.append(
                    RowFailure(
                        row_id=record["row_id"], stage=record["stage"], error=record["error"]
                    )
                )
    return outcomes


def read_oe_outcomes(path: Path) -> list[OEResolution | RowFailure]:
    """Read an oe_interchange JSONL (success and failure records)."""
    return read_outcomes(path, OEResolution)


def meta_path(output: Path) -> Path:
    return output.with_suffix(".meta.json")


def write_meta(output: Path, **info: Any) -> None:
    """Record how a run was produced (input, dataset, model) next to its output."""
    meta_path(output).write_text(json.dumps(info, indent=2, default=str) + "\n", encoding="utf-8")


def read_meta(output: Path) -> dict[str, Any]:
    path = meta_path(output)
    if not path.is_file():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
