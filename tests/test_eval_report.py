"""Evaluation reports (spec 14): markdown and PNG charts for one run and for a comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

from rfq_agent.eval.report import (
    render_compare_markdown,
    render_eval_markdown,
    write_compare_report,
    write_eval_report,
)
from tests.builders import run_eval

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_run_report_has_every_section_of_spec_14() -> None:
    text = render_eval_markdown(run_eval())
    for heading in (
        "# Evaluation: run-a",
        "## OE resolution",
        "## MSRP",
        "## Retail",
        "## Should-cost",
        "## Confidence calibration",
        "## Coverage-accuracy",
        "## Cost and time",
    ):
        assert heading in text, heading
    assert "| green | 1 | 1 | 1 |" in text  # band, rows, usable, row correct
    assert "| yellow | 1 | 0 | 0 |" in text
    assert "judged on their pre-review system result: 1 row" in text
    assert "| confidence_judge | 3 | 0 | 900 | 90 | $0.0040 |" in text
    assert "deepseek/deepseek-v4-pro" in text


def test_run_report_writes_markdown_and_two_png_charts(tmp_path: Path) -> None:
    paths = write_eval_report(run_eval(), tmp_path)
    assert sorted(p.name for p in paths) == [
        "eval_run-a.md",
        "eval_run-a_coverage.png",
        "eval_run-a_reliability.png",
    ]
    for p in paths:
        if p.suffix == ".png":
            assert p.read_bytes().startswith(PNG_MAGIC)
    md = (tmp_path / "eval_run-a.md").read_text()
    assert "eval_run-a_reliability.png" in md and "eval_run-a_coverage.png" in md


def test_compare_table_has_one_line_per_run() -> None:
    evals = [run_eval("run-a", "evidence"), run_eval("run-b", "evidence_plus_judge")]
    text = render_compare_markdown(evals)
    lines = [ln for ln in text.splitlines() if ln.startswith("| run-")]
    assert [ln.split(" | ")[1] for ln in lines] == ["evidence", "evidence_plus_judge"]
    assert "ECE" in text and "AUROC" in text and "per 1k rows" in text


def test_compare_writes_markdown_and_overlaid_charts(tmp_path: Path) -> None:
    evals = [run_eval("run-a"), run_eval("run-b", "evidence_plus_self")]
    paths = write_compare_report(evals, tmp_path, stamp="20261009T000000Z")
    assert sorted(p.name for p in paths) == [
        "compare_20261009T000000Z.md",
        "compare_20261009T000000Z_coverage.png",
        "compare_20261009T000000Z_reliability.png",
    ]


def test_compare_needs_at_least_two_runs() -> None:
    with pytest.raises(ValueError):
        render_compare_markdown([run_eval()])
