from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from rfq_agent.config import Pricing, Settings
from rfq_agent.llm.ping import PING_PROMPT, PingResponse
from rfq_agent.llm.structured import invoke_structured
from rfq_agent.llm.tracking import compute_cost
from rfq_agent.store.db import connect


def test_compute_cost() -> None:
    assert compute_cost(1_000_000, 500_000, Pricing(input=0.30, output=1.20)) == pytest.approx(0.90)
    assert compute_cost(100, 100, None) is None


async def test_call_is_recorded(settings: Settings) -> None:
    result = await invoke_structured(
        PingResponse,
        [HumanMessage(content=PING_PROMPT.format(token="abc123"))],
        role="judge",
        run_id="run-1",
        agent="oe_interchange",
        settings=settings,
    )
    assert result.ok and result.value is not None and result.value.echo == "abc123"

    with connect(settings.db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM llm_calls")]
    assert len(rows) == 1
    row = rows[0]
    assert row["rfq_run_id"] == "run-1"
    assert row["agent"] == "oe_interchange"
    assert row["role"] == "judge"
    assert (row["provider"], row["model"]) == ("fake", "fake")
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0
    assert row["cost_usd"] == 0.0
    assert row["latency_ms"] >= 0
    assert row["status"] == "ok"


async def test_failed_call_is_recorded_as_error(settings: Settings) -> None:
    class NoHandler(PingResponse):
        pass

    result = await invoke_structured(
        NoHandler, [HumanMessage(content="x")], role="default", run_id="run-2", settings=settings
    )
    assert not result.ok
    with connect(settings.db_path) as conn:
        row = dict(conn.execute("SELECT * FROM llm_calls").fetchone())
    assert row["status"] == "error"
    assert "FakeHandlerMissingError" in row["error"]
