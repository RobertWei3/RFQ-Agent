"""Evaluation metrics (spec 14): calibration, coverage-accuracy and the per-stage numbers."""

from __future__ import annotations

from typing import Any

import pytest

from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.eval.labels import RowLabel
from rfq_agent.eval.metrics import (
    TruthMismatch,
    auroc,
    calibration_eval,
    coverage_curve,
    ece,
    evaluate,
    injection_eval,
    msrp_eval,
    oe_eval,
    reliability,
    retail_eval,
    should_cost_eval,
    threshold_picks,
    usage_eval,
)
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import AgentUsage, Band, InjectionKind, ResolvedRow, RowFailure, RunEval
from tests.builders import breakdown, cost, line, msrp, oe, quote, retail, truth


def test_ece_weights_each_bin_gap_by_its_share_of_rows() -> None:
    # bin [90, 100]: mean 0.9, usable 1/2 -> gap 0.4, weight 2/4
    # bin [10, 20):  mean 0.1, usable 0/2 -> gap 0.1, weight 2/4
    assert ece([90, 95, 10, 15], [True, False, False, False]) == pytest.approx(
        0.5 * abs(0.925 - 0.5) + 0.5 * abs(0.125 - 0.0)
    )


def test_ece_is_zero_when_scores_match_the_rates() -> None:
    assert ece([100, 0], [True, False]) == 0.0


def test_reliability_bins_put_100_in_the_last_bin_and_keep_empty_bins() -> None:
    bins = reliability([100, 90, 55, 0], [True, False, True, False])
    assert [(b.lo, b.hi) for b in bins] == [(i * 10, i * 10 + 10) for i in range(10)]
    top, mid, bottom = bins[9], bins[5], bins[0]
    assert (top.rows, top.mean_score, top.usable_rate) == (2, pytest.approx(0.95), 0.5)
    assert (mid.rows, mid.mean_score, mid.usable_rate) == (1, pytest.approx(0.55), 1.0)
    assert (bottom.rows, bottom.mean_score, bottom.usable_rate) == (1, 0.0, 0.0)
    assert (bins[3].rows, bins[3].mean_score, bins[3].usable_rate) == (0, None, None)


def test_auroc_counts_ties_as_half() -> None:
    # positives 80, 60; negatives 60, 20 -> pairs 1 + 1 + 0.5 + 1 = 3.5 of 4
    assert auroc([80, 60, 60, 20], [True, True, False, False]) == pytest.approx(0.875)


def test_auroc_is_none_with_a_single_class() -> None:
    assert auroc([80, 60], [True, True]) is None


def test_coverage_curve_handles_rows_at_or_above_each_threshold() -> None:
    curve = coverage_curve([90, 70, 40, 40], [True, False, True, False])
    assert len(curve) == 101
    at = {p.threshold: p for p in curve}
    assert (at[0].coverage, at[0].accuracy) == (1.0, 0.5)
    assert (at[41].coverage, at[41].accuracy) == (0.5, 0.5)
    assert (at[71].coverage, at[71].accuracy) == (0.25, 1.0)
    assert (at[91].coverage, at[91].accuracy) == (0.0, None)


def test_threshold_picks_take_the_lowest_threshold_reaching_each_target() -> None:
    curve = coverage_curve([90, 70, 40, 40], [True, False, True, False])
    picks = {p.target: p for p in threshold_picks(curve, (0.5, 0.9))}
    assert (picks[0.5].threshold, picks[0.5].coverage) == (0, 1.0)
    assert (picks[0.9].threshold, picks[0.9].coverage) == (71, 0.25)


def test_threshold_pick_is_empty_when_no_threshold_reaches_the_target() -> None:
    curve = coverage_curve([90], [False])
    [pick] = threshold_picks(curve, (0.9,))
    assert (pick.threshold, pick.coverage) == (None, None)


# --- per-stage metrics ------------------------------------------------------------------


def row(row_id: int, **paths: Any) -> ResolvedRow:
    outcomes = {
        "oe": oe(row_id),
        "msrp": msrp(row_id),
        "retail": retail(row_id),
        "cost": cost(row_id),
        **paths,
    }
    [resolved] = resolve_rows(
        [line(row_id)],
        [outcomes["oe"]],
        [outcomes["msrp"]],
        [outcomes["retail"]],
        [outcomes["cost"]],
    )
    return resolved


def not_found_truth(row_id: int) -> GroundTruthRow:
    t = truth(row_id, oe_status_expected="not_found", primary_oe=None, accepted_oes=[])
    t.supersession_chain = []
    return t


def test_oe_metrics() -> None:
    rows = [
        row(1),  # right OE, right chain
        row(2, oe=oe(2, supersession_chain=["NEW-3"])),  # right OE, chain misses OLD-1
        row(3, oe=oe(3, "WRONG-9")),  # resolved to the wrong number
        row(4, oe=oe(4, None)),  # correct not_found
        row(5, oe=RowFailure(row_id=5, stage="search_chain", error="timeout")),
    ]
    truths = [truth(1), truth(2), truth(3), not_found_truth(4), truth(5)]
    result = oe_eval(rows, truths)
    assert result.coverage == pytest.approx(3 / 5)
    assert result.accuracy == pytest.approx(2 / 3)
    assert result.conclusion_accuracy == pytest.approx(3 / 5)
    # both sides resolved: rows 1-3; row 3's chain ends in the wrong number
    assert result.chain_accuracy == pytest.approx(1 / 3)


def test_msrp_metrics() -> None:
    rows = [
        row(1),  # 100 vs 100
        row(2, msrp=msrp(2, 103.0)),  # 3% off
        row(3, msrp=msrp(3, None)),  # not found, truth says found
        row(4, msrp=msrp(4, 100.5, status="discontinued")),  # truth discontinued, 0.5% off
    ]
    t4 = truth(4)
    t4.msrp.status = "discontinued"
    result = msrp_eval(rows, [truth(1), truth(2), truth(3), t4])
    assert result.coverage == pytest.approx(3 / 4)
    assert result.accuracy == pytest.approx(2 / 3)
    assert result.status_accuracy == pytest.approx(3 / 4)
    assert result.mean_rel_error == pytest.approx((0 + 0.03 + 0.005) / 3)


def test_retail_metrics() -> None:
    # row 1: quotes from sites 1-3 as truth lists, retail_1 selected (right)
    # row 2: site 3 missed, retail_2 flagged as outlier but truth has none, retail_1 selected
    # row 3: truth flags retail_3 as outlier, system flags it too, but selects retail_2
    t3 = truth(3)
    t3.retail.outlier_sites = ["retail_3"]
    rows = [
        row(1),
        row(2, retail=retail(2, {"retail_1": 80.0, "retail_2": 82.0}, outliers=["retail_2"])),
        row(3, retail=retail(3, outliers=["retail_3"], selected=quote("retail_2", 82.0))),
    ]
    result = retail_eval(rows, [truth(1), truth(2), t3])
    site3 = result.sites["retail_3"]
    assert (site3.expected, site3.quoted, site3.found) == (3, 2, 2)
    assert site3.coverage == pytest.approx(2 / 3)
    assert result.sites["retail_4"].expected == 0 and result.sites["retail_4"].coverage is None
    assert result.selected_accuracy == pytest.approx(2 / 3)
    # predicted (2, retail_2), (3, retail_3); true (3, retail_3)
    assert result.outlier_precision == pytest.approx(1 / 2)
    assert result.outlier_recall == pytest.approx(1.0)


def test_outlier_precision_is_none_without_predicted_outliers() -> None:
    result = retail_eval([row(1)], [truth(1)])
    assert result.outlier_precision is None and result.outlier_recall is None


def test_should_cost_metrics() -> None:
    known = truth(1)
    known.cost = breakdown(fob=20.0, landed=30.0)
    off = truth(2)
    off.cost = breakdown(fob=20.0, landed=25.0)  # system says 30 -> 20% error
    unknown = truth(3, our_part_reachability="unreachable")  # category default cycle time
    unknown.cost = breakdown(fob=20.0, landed=40.0)  # system says 30 -> 25% error
    rows = [
        row(1),
        row(2),
        row(3, cost=cost(3, weight_kg=0.5)),  # wrong weight
    ]
    result = should_cost_eval(rows, [known, off, unknown])
    assert result.spec_accuracy == pytest.approx(2 / 3)
    assert result.landed_mape_part == pytest.approx((0 + 0.2) / 2)
    assert result.landed_mape_default == pytest.approx(0.25)
    assert (result.n_part, result.n_default) == (2, 1)


def scored(row_id: int, score: int, band: Band) -> ResolvedRow:
    r = row(row_id)
    sheet = r.worksheet.model_copy(update={"confidence_score": score, "band": band})
    return r.model_copy(update={"worksheet": sheet})


def test_calibration_summarizes_bands_and_the_red_rows_held_back() -> None:
    rows = [scored(1, 90, "green"), scored(2, 85, "green"), scored(3, 60, "yellow")]
    rows += [scored(4, 30, "red"), scored(5, 20, "red")]
    labels = [
        RowLabel(1, row_correct=True, usable=True),
        RowLabel(2, row_correct=False, usable=False),
        RowLabel(3, row_correct=True, usable=True),
        RowLabel(4, row_correct=True, usable=False),  # e.g. a correct not_found
        RowLabel(5, row_correct=False, usable=False),
    ]
    result = calibration_eval(rows, labels)
    assert result.row_correct_rate == pytest.approx(3 / 5)
    assert result.usable_rate == pytest.approx(2 / 5)
    assert result.bands["green"].model_dump(exclude={"schema_version"}) == {
        "rows": 2,
        "usable": 1,
        "row_correct": 1,
    }
    assert result.bands["red"].rows == 2 and result.red_row_correct_rate == 0.5
    # usable scores 90, 60; not usable 85, 30, 20 -> pairs won 3 + 2 = 5 of 6
    assert result.auroc == pytest.approx(5 / 6)
    assert [p.target for p in result.thresholds] == [0.90, 0.95, 0.99]
    assert len(result.reliability) == 10 and len(result.curve) == 101


def test_red_rate_is_none_without_red_rows() -> None:
    result = calibration_eval([scored(1, 90, "green")], [RowLabel(1, True, True)])
    assert result.red_row_correct_rate is None and result.bands["red"].rows == 0


def usage(agent: str, cost_usd: float | None, calls: int = 10) -> AgentUsage:
    return AgentUsage(
        agent=agent, calls=calls, errors=0, input_tokens=1, output_tokens=1, cost_usd=cost_usd
    )


def test_usage_adds_the_source_run_and_scales_per_thousand_rows() -> None:
    result = usage_eval(
        [usage("confidence_judge", 0.02), usage("confidence_self_eval", 0.01)],
        {"judge": "deepseek/deepseek-v4-pro"},
        rows=200,
        elapsed_s=12.0,
        source=[usage("oe_interchange", 0.5)],
    )
    assert result.cost_usd == pytest.approx(0.03)
    assert result.source_cost_usd == pytest.approx(0.5)
    assert result.total_cost_usd == pytest.approx(0.53)
    assert result.cost_per_1k_rows == pytest.approx(2.65)


def test_usage_cost_is_none_when_a_call_is_unpriced() -> None:
    result = usage_eval([usage("a", None), usage("b", 0.1)], {}, rows=10, elapsed_s=None)
    assert result.cost_usd is None and result.total_cost_usd is None
    assert result.source_cost_usd is None and result.cost_per_1k_rows is None


def test_usage_without_calls_costs_nothing() -> None:
    result = usage_eval([], {}, rows=10, elapsed_s=1.0)
    assert (result.cost_usd, result.total_cost_usd, result.cost_per_1k_rows) == (0.0, 0.0, 0.0)


def test_evaluate_labels_every_row_in_row_order() -> None:
    rows = [scored(2, 60, "yellow"), scored(1, 90, "green")]
    rows[0] = row(2, msrp=msrp(2, 150.0)).model_copy(update={"worksheet": rows[0].worksheet})
    result = evaluate(
        rows,
        [truth(1), truth(2)],
        run_id="run-1",
        strategy="evidence",
        truth_path="gt.jsonl",
        reviewed_rows=0,
        usage=usage_eval([], {}, rows=2, elapsed_s=None),
        created_at="2026-10-09T00:00:00+00:00",
    )
    assert result.rows == 2 and result.strategy == "evidence"
    assert [(r.row_id, r.score, r.band, r.row_correct, r.usable) for r in result.row_evals] == [
        (1, 90, "green", True, True),
        (2, 60, "yellow", False, False),
    ]
    assert result.calibration.bands["green"].usable == 1


@pytest.mark.parametrize(
    "truths",
    [
        [truth(1)],  # a row has no truth
        [truth(1), truth(2, part="CP-99")],  # customer part numbers differ
    ],
)
def test_evaluate_refuses_ground_truth_of_another_rfq(truths: list[GroundTruthRow]) -> None:
    with pytest.raises(TruthMismatch):
        evaluate(
            [row(1), row(2)],
            truths,
            run_id="run-1",
            strategy="evidence",
            truth_path="gt.jsonl",
            reviewed_rows=0,
            usage=usage_eval([], {}, rows=2, elapsed_s=None),
            created_at="2026-10-09T00:00:00+00:00",
        )


def test_evaluate_leaves_out_truth_rows_beyond_a_limited_run() -> None:
    result = evaluate(
        [row(1)],
        [truth(1), truth(2)],
        run_id="run-1",
        strategy="evidence",
        truth_path="gt.jsonl",
        reviewed_rows=0,
        usage=usage_eval([], {}, rows=1, elapsed_s=None),
        created_at="2026-10-09T00:00:00+00:00",
    )
    assert result.rows == 1 and [r.row_id for r in result.row_evals] == [1]


def test_injection_eval_counts_bands_and_how_well_scores_separate_the_injected_rows() -> None:
    rows = [scored(1, 92, "green"), scored(2, 85, "green"), scored(3, 70, "yellow")]
    rows += [scored(4, 88, "green"), scored(5, 40, "red"), scored(6, 60, "yellow")]
    labels = [
        RowLabel(1, row_correct=True, usable=True),
        RowLabel(2, row_correct=True, usable=True),
        RowLabel(3, row_correct=True, usable=True),
        RowLabel(4, row_correct=False, usable=False),  # injected oe, still green
        RowLabel(5, row_correct=False, usable=False),  # a correct-but-unusable red row
        RowLabel(6, row_correct=False, usable=False),  # injected msrp, yellow
    ]
    result = injection_eval(rows, labels, {4: "oe", 6: "msrp"})
    assert result.rows == 2 and result.green_injected == 1
    assert result.held_back_rate == 0.5
    assert result.bands_by_kind["oe"] == {"green": 1, "yellow": 0, "red": 0}
    assert result.bands_by_kind["msrp"] == {"green": 0, "yellow": 1, "red": 0}
    assert result.bands_by_kind["retail"] == {"green": 0, "yellow": 0, "red": 0}
    # usable non-injected 92, 85, 70 vs injected 88, 60 (row 5 is in neither group):
    # 92>88, 92>60, 85<88, 85>60, 70<88, 70>60 -> 4 of 6 pairs
    assert result.auroc == pytest.approx(4 / 6)


def test_evaluate_reports_injection_only_for_injected_runs() -> None:
    def run(injections: dict[int, InjectionKind] | None) -> RunEval:
        return evaluate(
            [scored(1, 90, "green")],
            [truth(1)],
            run_id="run-1",
            strategy="evidence",
            truth_path="gt.jsonl",
            reviewed_rows=0,
            usage=usage_eval([], {}, rows=1, elapsed_s=None),
            created_at="2026-10-10T00:00:00+00:00",
            injections=injections,
        )

    assert run(None).injection is None
    plain = run({})
    assert plain.injection is None
    with_one = run({1: "oe"})
    assert with_one.injection is not None and with_one.injection.rows == 1


def test_an_injected_row_without_a_band_is_not_counted_as_held_back() -> None:
    unbanded = scored(2, 0, "red")
    unbanded = unbanded.model_copy(
        update={"worksheet": unbanded.worksheet.model_copy(update={"band": None})}
    )
    rows = [scored(1, 60, "yellow"), unbanded]
    labels = [RowLabel(1, False, False), RowLabel(2, False, False)]
    result = injection_eval(rows, labels, {1: "oe", 2: "msrp"})
    assert result.held_back_rate == 0.5  # only the yellow row is held back
