"""``dashboard.charts``: API JSON -> what the pages draw (spec 15.2)."""

from __future__ import annotations

from typing import Any

from dashboard import charts
from rfq_agent.eval.report import TARGET_FOR_COMPARE, render_compare_markdown
from rfq_agent.schemas import StrategyName
from tests.builders import run_eval

BREAKDOWN = {
    "material": 10.0,
    "conversion": 4.0,
    "overhead": 2.0,
    "sga": 1.0,
    "profit": 2.0,
    "packaging": 1.0,
    "fob": 20.0,
    "freight": 3.0,
    "insurance": 0.5,
    "duty": 5.0,
    "mpf": 0.5,
    "brokerage": 0.25,
    "inland_freight": 0.75,
    "landed_cost": 30.0,
}


def test_the_cost_waterfall_steps_from_material_through_fob_to_landed_cost() -> None:
    steps = charts.waterfall(BREAKDOWN)
    assert [(s["item"], s["start"], s["end"], s["kind"]) for s in steps] == [
        ("material", 0.0, 10.0, "cost"),
        ("conversion", 10.0, 14.0, "cost"),
        ("overhead", 14.0, 16.0, "cost"),
        ("sga", 16.0, 17.0, "cost"),
        ("profit", 17.0, 19.0, "cost"),
        ("packaging", 19.0, 20.0, "cost"),
        ("fob", 0.0, 20.0, "subtotal"),
        ("freight", 20.0, 23.0, "cost"),
        ("insurance", 23.0, 23.5, "cost"),
        ("duty", 23.5, 28.5, "cost"),
        ("mpf", 28.5, 29.0, "cost"),
        ("brokerage", 29.0, 29.25, "cost"),
        ("inland_freight", 29.25, 30.0, "cost"),
        ("landed_cost", 0.0, 30.0, "total"),
    ]
    assert [s["order"] for s in steps] == list(range(14))


def _quote(source: str, price: float, **kw: object) -> dict[str, object]:
    return {
        "source": source,
        "list_price": price,
        "sale_price": None,
        "discount_pct": None,
        "effective_price": price,
        "in_stock": True,
        "match_quality": "exact",
        "matched_part_no": "NEW-3",
        **kw,
    }


def test_site_prices_mark_the_selected_quote_and_the_outliers() -> None:
    retail = {
        "quotes": [
            _quote("retailer_1", 20.0),
            _quote("retailer_2", 22.0),
            _quote("retailer_3", 90.0),
        ],
        "selected": _quote("retailer_1", 20.0),
        "outliers": ["retailer_3"],
    }
    bars = charts.site_prices(retail)
    assert [(b["site"], b["effective_price"], b["role"]) for b in bars] == [
        ("retailer_1", 20.0, "selected"),
        ("retailer_2", 22.0, "other"),
        ("retailer_3", 90.0, "outlier"),
    ]


def test_site_prices_of_a_failed_or_unpriced_row_are_empty() -> None:
    assert charts.site_prices({"stage": "search", "error": "timeout", "row_id": 1}) == []
    assert charts.site_prices({"quotes": [], "selected": None, "outliers": []}) == []


def test_a_stage_failure_is_told_apart_from_a_result() -> None:
    assert charts.is_failure({"row_id": 1, "stage": "upstream_oe", "error": "OE failed"})
    assert not charts.is_failure({"row_id": 1, "status": "found", "quote": None})


def test_signals_split_into_evidence_gates_and_llm_parts() -> None:
    signals = {
        "oe_match": 0.7,
        "chain": 1.0,
        "price_sources": 0.75,
        "retail_dispersion": 0.5,
        "msrp_retail": 1.0,
        "spec_source": 0.3,
        "gate_oe_multiple": 49.0,
        "judge_agree": 0.0,
        "judge_score": 40.0,
    }
    parts = charts.split_signals(signals)
    assert parts.evidence == [
        ("oe_match", 0.7),
        ("chain", 1.0),
        ("price_sources", 0.75),
        ("retail_dispersion", 0.5),
        ("msrp_retail", 1.0),
        ("spec_source", 0.3),
    ]
    assert parts.gates == [("oe_multiple", 49)]
    assert parts.llm == [("judge_agree", 0.0), ("judge_score", 40.0)]


def _worksheet_row(row_id: int, band: str | None, **kw: object) -> dict[str, object]:
    return {
        "row_id": row_id,
        "customer_part_no_raw": f" CP-{row_id} ",
        "customer_part_no": f"CP{row_id}",
        "our_part_no": None,
        "oe_status": "resolved",
        "current_oe": "NEW-3",
        "supersession_chain": ["OLD-1", "NEW-3"],
        "oe_candidates": [],
        "shared_oe_rows": [],
        "price_source_row": None,
        "shared_prices_disagree": False,
        "msrp": 30.0,
        "msrp_status": "found",
        "retail_prices": {"retailer_2": 22.0, "retailer_1": None},
        "selected_retail_price": 22.0,
        "selected_retail_source": "retailer_2",
        "retail_outliers": [],
        "should_cost_fob": 9.5,
        "landed_cost": 12.0,
        "confidence_score": 85,
        "band": band,
        "review_reasons": ["a", "b"],
        "failures": [],
        "reasoning_summary": {"oe": "x"},
        "review_status": None,
        **kw,
    }


def test_the_worksheet_table_has_the_excel_columns_with_one_column_per_site() -> None:
    frame = charts.worksheet_frame([_worksheet_row(1, "green"), _worksheet_row(2, "red")])
    assert list(frame.columns[:12]) == [
        "row_id",
        "customer_part_no_raw",
        "customer_part_no",
        "our_part_no",
        "oe_status",
        "current_oe",
        "supersession_chain",
        "oe_candidates",
        "msrp",
        "selected_retail_price",
        "landed_cost",
        "msrp_status",
    ]
    assert list(frame.columns[12:14]) == ["retail_price_retailer_1", "retail_price_retailer_2"]
    first = frame.iloc[0]
    assert first["supersession_chain"] == "OLD-1 -> NEW-3"
    assert first["retail_price_retailer_2"] == 22.0
    assert first["review_reasons"] == "a\nb"
    assert first["band"] == "green"


def test_worksheet_rows_are_coloured_by_band_like_the_excel_sheet() -> None:
    rows = [_worksheet_row(1, "green"), _worksheet_row(2, "yellow"), _worksheet_row(3, "red")]
    rows.append(_worksheet_row(4, None, confidence_score=None))
    frame = charts.worksheet_frame(rows)
    assert charts.band_colors(frame) == ["#C6EFCE", "#FFEB9C", "#FFC7CE", None]
    html = charts.styled_worksheet(frame).to_html()
    assert "background-color: #FFC7CE" in html


def test_an_empty_page_is_an_empty_table() -> None:
    assert charts.worksheet_frame([]).empty


def test_worksheet_prices_show_two_decimals_and_blanks_as_a_dash() -> None:
    html = charts.styled_worksheet(charts.worksheet_frame([_worksheet_row(1, "green")])).to_html()
    assert ">30.00<" in html and ">9.50<" in html
    assert "30.000000" not in html
    assert ">—<" in html  # retail_1 has no price


def test_the_waterfall_colours_costs_subtotal_and_total_apart() -> None:
    spec = charts.waterfall_chart(BREAKDOWN).to_dict()
    scale = spec["encoding"]["color"]["scale"]
    assert scale["domain"] == ["cost", "subtotal", "total"]
    assert len(set(scale["range"])) == 3


# --- evaluation page (spec 14, 15.2) ----------------------------------------------------------


def _eval_json(run_id: str = "run-a", strategy: StrategyName = "evidence") -> dict[str, Any]:
    data: dict[str, Any] = run_eval(run_id, strategy).model_dump(mode="json")
    return data


def test_reliability_points_skip_empty_bins_and_name_the_run() -> None:
    points = charts.reliability_points([_eval_json()])
    # run_eval: scores 92 (usable) and 64 (not usable)
    assert points == [
        {"run": "run-a (evidence)", "mean_score": 0.64, "usable_rate": 0.0, "rows": 1},
        {"run": "run-a (evidence)", "mean_score": 0.92, "usable_rate": 1.0, "rows": 1},
    ]


def test_curve_points_give_coverage_and_accuracy_per_threshold() -> None:
    points = charts.curve_points([_eval_json()])
    at = {(p["threshold"], p["metric"]): p["value"] for p in points}
    assert at[(0, "覆盖率")] == 1.0 and at[(0, "准确率")] == 0.5
    assert at[(65, "覆盖率")] == 0.5 and at[(65, "准确率")] == 1.0
    assert (93, "准确率") not in at  # no rows left: no accuracy point
    assert at[(93, "覆盖率")] == 0.0


def test_the_comparison_table_has_one_row_per_run() -> None:
    frame = charts.compare_frame([_eval_json("run-a"), _eval_json("run-b", "evidence_plus_judge")])
    assert list(frame["运行"]) == ["run-a", "run-b"]
    assert list(frame["策略"]) == ["evidence", "evidence_plus_judge"]
    assert list(frame["绿 / 黄 / 红"]) == ["1 / 1 / 0", "1 / 1 / 0"]
    assert list(frame["可直接使用率"]) == [0.5, 0.5]
    for column in ("ECE", "AUROC", "95% 阈值", "95% 覆盖率", "合计成本", "每千行成本"):
        assert column in frame.columns


def test_evaluation_charts_are_vega_lite_specs() -> None:
    evals = [_eval_json()]
    reliability = charts.reliability_chart(charts.reliability_points(evals)).to_dict()
    coverage = charts.coverage_chart(charts.curve_points(evals)).to_dict()
    assert "layer" in reliability and "layer" in coverage


def test_the_threshold_reaching_the_target_accuracy_is_marked_on_the_curve() -> None:
    evals = [_eval_json()]
    # scores 92 (usable) and 64 (not): accuracy first reaches 95% at threshold 65
    marks = charts.threshold_marks(evals)
    assert marks == [{"run": "run-a (evidence)", "threshold": 65, "label": "t=65 (95%)"}]
    spec = charts.coverage_chart(charts.curve_points(evals), marks).to_dict()
    assert any(
        layer.get("mark", {}).get("type") == "rule" and "x" in layer.get("encoding", {})
        for layer in spec["layer"]
    )


def test_no_mark_when_the_target_accuracy_is_never_reached() -> None:
    ev = _eval_json()
    for pick in ev["calibration"]["thresholds"]:
        pick["threshold"], pick["coverage"] = None, None
    assert charts.threshold_marks([ev]) == []


# Report column -> dashboard columns. The two tables are written separately (the dashboard
# may not import the backend); this keeps them in step: a column added to one side only
# fails here.
COMPARE_COLUMNS = {
    "run": ["运行"],
    "strategy": ["策略"],
    "models": ["模型"],
    "rows": ["行数"],
    "green / yellow / red": ["绿 / 黄 / 红"],
    "row correct": ["行正确率"],
    "usable": ["可直接使用率"],
    "ECE": ["ECE"],
    "AUROC": ["AUROC"],
    "green usable": ["绿档可直接使用"],
    "red row correct": ["红档行正确"],
    f"threshold @{TARGET_FOR_COMPARE:.0%} (coverage)": [
        f"{TARGET_FOR_COMPARE:.0%} 阈值",
        f"{TARGET_FOR_COMPARE:.0%} 覆盖率",
    ],
    "injected held back": ["注入行被拦下"],
    "injected in green": ["绿档注入行"],
    "injection AUROC": ["注入 AUROC"],
    "LLM calls (this run)": ["本运行调用"],
    "cost (this run)": ["本运行成本"],
    "total cost": ["合计成本"],
    "per 1k rows": ["每千行成本"],
    "elapsed": ["耗时（秒）"],
}


def test_the_dashboard_comparison_matches_the_report_comparison() -> None:
    evals = [run_eval("run-a"), run_eval("run-b", "evidence_plus_judge")]
    header = render_compare_markdown(evals).splitlines()[2]
    assert [c.strip() for c in header.strip("|").split("|")] == list(COMPARE_COLUMNS)
    frame = charts.compare_frame([e.model_dump(mode="json") for e in evals])
    assert list(frame.columns) == [c for cols in COMPARE_COLUMNS.values() for c in cols]
    assert charts.COMPARE_TARGET == TARGET_FOR_COMPARE
