"""Runner and CLI tests: real MCP server via langchain-mcp-adapters, fake LLM."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rfq_agent.agents.runner import mcp_our_part_lookup, run_oe_interchange
from rfq_agent.cli import app
from rfq_agent.config import Settings
from rfq_agent.data_gen.audit import load_truth
from rfq_agent.intake import read_rfq
from rfq_agent.schemas import OEResolution


async def test_mcp_lookup_via_adapters(settings: Settings, generated_dataset: Path) -> None:
    truth = load_truth(generated_dataset)
    xref = next(t for t in truth if t.our_part_reachability == "xref")
    unreachable = next(t for t in truth if t.our_part_reachability == "unreachable")
    async with mcp_our_part_lookup(settings, generated_dataset) as lookup:
        assert await lookup(f" {xref.customer_part_no_raw.lower()} ") == xref.our_part_no
        assert await lookup(unreachable.customer_part_no) is None


async def test_runner_on_sample(settings: Settings, generated_dataset: Path) -> None:
    lines = read_rfq(generated_dataset / "rfq_sample_20.xlsx").lines
    outcomes = await run_oe_interchange(
        lines, settings, dataset_dir=generated_dataset, run_id="sample"
    )
    assert len(outcomes) == 20
    assert all(isinstance(o, OEResolution) for o in outcomes)


@pytest.fixture
def cli_config(config_dir: Path, generated_dataset: Path) -> Path:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["data"]["dataset_dir"] = str(generated_dataset)
    path.write_text(yaml.safe_dump(data))
    return config_dir


@pytest.mark.usefixtures("cli_config")
def test_cli_writes_jsonl_and_comparison(generated_dataset: Path, tmp_path: Path) -> None:
    out = tmp_path / "oe.jsonl"
    result = CliRunner().invoke(
        app,
        [
            "agent",
            "oe-interchange",
            "--input",
            str(generated_dataset / "rfq_sample_20.xlsx"),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["row_id"] for r in records] == list(range(1, 21))
    assert all(r["ok"] for r in records)
    OEResolution.model_validate(records[0]["result"])
    report = out.with_suffix(".compare.md").read_text()
    assert "| row | customer part |" in report
    assert "OE correct 20/20" in report
    assert "LLM usage: calls=" in result.output


@pytest.mark.usefixtures("cli_config")
def test_cli_refuses_real_provider_without_flag(
    generated_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RFQ_LLM_PROVIDER")
    result = CliRunner().invoke(
        app,
        ["agent", "oe-interchange", "--input", str(generated_dataset / "rfq_sample_20.xlsx")],
    )
    assert result.exit_code == 2
    assert "--allow-real" in result.output
    assert "expected ~60" in result.output


@pytest.mark.usefixtures("cli_config")
def test_cli_writes_trace_and_trace_show(generated_dataset: Path, tmp_path: Path) -> None:
    out = tmp_path / "oe.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "agent",
            "oe-interchange",
            "--input",
            str(generated_dataset / "rfq_sample_20.xlsx"),
            "--output",
            str(out),
            "--limit",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    trace_path = out.with_suffix(".trace.jsonl")
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    steps_row1 = [e["step"] for e in events if e["row_id"] == 1]
    assert steps_row1[0] == "start"
    assert steps_row1[-1] == "build_resolution"
    assert {"lookup_our_part", "search", "llm_call", "extract_candidates"} <= set(steps_row1)
    llm = next(e for e in events if e["step"] == "llm_call")
    assert llm["data"]["prompt"][0]["role"] == "system"
    assert llm["data"]["response"]["tool_calls"]

    listing = runner.invoke(app, ["trace", "show", str(trace_path)])
    assert listing.exit_code == 0 and "llm calls" in listing.output
    detail = runner.invoke(app, ["trace", "show", str(trace_path), "--row", "1"])
    assert "oe_interchange · llm_call" in detail.output
    assert "▸ system:" in detail.output


@pytest.mark.usefixtures("cli_config")
def test_cli_no_trace(generated_dataset: Path, tmp_path: Path) -> None:
    out = tmp_path / "oe.jsonl"
    result = CliRunner().invoke(
        app,
        [
            "agent",
            "oe-interchange",
            "--input",
            str(generated_dataset / "rfq_sample_20.xlsx"),
            "--output",
            str(out),
            "--limit",
            "1",
            "--no-trace",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not out.with_suffix(".trace.jsonl").exists()
