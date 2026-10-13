"""``rfq worksheet build``: resolution + confidence over P2-P5 agent outputs (fake LLM)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from rfq_agent.cli import app
from rfq_agent.schemas import ResolvedRow

_UPSTREAM: dict[str, Path] = {}


@pytest.fixture
def cli_config(config_dir: Path, generated_dataset: Path) -> Path:
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["data"]["dataset_dir"] = str(generated_dataset)
    path.write_text(yaml.safe_dump(data))
    return config_dir


@pytest.fixture
def upstream(
    cli_config: Path, generated_dataset: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Path]:
    """The four agent outputs for the 20-row sample, produced once per session."""
    if _UPSTREAM:
        return _UPSTREAM
    out = tmp_path_factory.mktemp("upstream")
    runner = CliRunner()
    sample = str(generated_dataset / "rfq_sample_20.xlsx")
    files = {name: out / f"{name}.jsonl" for name in ("oe", "msrp", "retail", "cost")}
    commands = [
        ["agent", "oe-interchange", "--input", sample, "--output", str(files["oe"])],
        ["agent", "oe-msrp", "--from-oe", str(files["oe"]), "--output", str(files["msrp"])],
        ["agent", "retail-price", "--from-oe", str(files["oe"]), "--output", str(files["retail"])],
        ["agent", "should-cost", "--from-oe", str(files["oe"]), "--output", str(files["cost"])],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
    _UPSTREAM.update(files)
    return _UPSTREAM


def build_args(files: dict[str, Path], out: Path, *extra: str) -> list[str]:
    return [
        "worksheet",
        "build",
        "--oe",
        str(files["oe"]),
        "--msrp",
        str(files["msrp"]),
        "--retail",
        str(files["retail"]),
        "--cost",
        str(files["cost"]),
        "--output",
        str(out),
        *extra,
    ]


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("strategy", ["evidence", "evidence_plus_self", "evidence_plus_judge"])
def test_every_strategy_runs_over_the_sample(
    upstream: dict[str, Path], tmp_path: Path, strategy: str
) -> None:
    out = tmp_path / "worksheet.jsonl"
    result = CliRunner().invoke(app, build_args(upstream, out, "--strategy", strategy))

    assert result.exit_code == 0, result.output
    rows = [ResolvedRow.model_validate(r) for r in records(out)]
    assert [r.line.row_id for r in rows] == list(range(1, 21))
    assert all(r.confidence and r.confidence.strategy == strategy for r in rows)
    assert all(r.worksheet.band is not None for r in rows)
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["agent"] == "worksheet" and meta["strategy"] == strategy
    assert "bands: green" in result.output


def test_sample_many_to_one_and_one_to_many_rows(upstream: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "worksheet.jsonl"
    result = CliRunner().invoke(app, build_args(upstream, out))
    assert result.exit_code == 0, result.output

    rows = {r.line.row_id: r.worksheet for r in map(ResolvedRow.model_validate, records(out))}
    shared = [ws for ws in rows.values() if ws.shared_oe_rows]
    assert shared, "the sample has a many-to-one pair"
    for ws in shared:
        partner = rows[ws.shared_oe_rows[0]]
        assert partner.current_oe == ws.current_oe
        assert partner.msrp == ws.msrp
        assert partner.selected_retail_source == ws.selected_retail_source
    multiple = [ws for ws in rows.values() if ws.oe_status == "multiple"]
    assert multiple, "the sample has one-to-many rows"
    assert all(ws.band == "red" and len(ws.oe_candidates) >= 2 for ws in multiple)


def test_evidence_needs_no_llm_and_writes_the_comparison(
    upstream: dict[str, Path], tmp_path: Path, generated_dataset: Path
) -> None:
    out = tmp_path / "worksheet.jsonl"
    truth = generated_dataset / "rfq_sample_20_ground_truth.jsonl"
    result = CliRunner().invoke(
        app, build_args(upstream, out, "--strategy", "evidence", "--truth", str(truth))
    )
    assert result.exit_code == 0, result.output
    assert "LLM calls: expected ~0, upper bound 0" in result.output
    report = out.with_suffix(".compare.md").read_text()
    assert "| row | score | band | row correct |" in report
    assert "row correct" in report.splitlines()[-1]


def test_rfq_defaults_to_the_workbook_recorded_upstream(
    upstream: dict[str, Path], tmp_path: Path, generated_dataset: Path
) -> None:
    out = tmp_path / "worksheet.jsonl"
    result = CliRunner().invoke(app, build_args(upstream, out))
    assert result.exit_code == 0, result.output
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["rfq"].endswith("rfq_sample_20.xlsx")


def test_mismatched_inputs_are_refused(upstream: dict[str, Path], tmp_path: Path) -> None:
    short = tmp_path / "msrp_short.jsonl"
    short.write_text("\n".join(upstream["msrp"].read_text().splitlines()[:5]) + "\n")
    files = {**upstream, "msrp": short}
    result = CliRunner().invoke(app, build_args(files, tmp_path / "w.jsonl"))
    assert result.exit_code == 2
    assert "msrp: missing [6," in result.output


@pytest.mark.parametrize("strategy", ["evidence_plus_self", "evidence_plus_judge"])
def test_announced_calls_match_the_calls_made(
    upstream: dict[str, Path], tmp_path: Path, strategy: str
) -> None:
    out = tmp_path / "worksheet.jsonl"
    result = CliRunner().invoke(app, build_args(upstream, out, "--strategy", strategy))
    assert result.exit_code == 0, result.output

    expected = re.search(r"LLM calls: expected ~(\d+),", result.output)
    made = re.search(r"LLM usage: calls=(\d+) ", result.output)
    assert expected and made
    assert expected.group(1) == made.group(1)


def test_limit_builds_only_the_first_rows(upstream: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "worksheet.jsonl"
    result = CliRunner().invoke(app, build_args(upstream, out, "--limit", "5"))
    assert result.exit_code == 0, result.output
    assert [r["line"]["row_id"] for r in records(out)] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("strategy", ["evidence_plus_self", "evidence_plus_judge"])
def test_real_provider_needs_allow_real(
    upstream: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
) -> None:
    from rfq_agent.config import get_settings

    monkeypatch.setenv("RFQ_LLM_PROVIDER", "deepseek")
    get_settings.cache_clear()
    out = tmp_path / "worksheet.jsonl"
    result = CliRunner().invoke(app, build_args(upstream, out, "--strategy", strategy))
    assert result.exit_code == 2
    assert "--allow-real" in result.output
    assert not out.exists()
