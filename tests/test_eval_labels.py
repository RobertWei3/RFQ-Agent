"""The two evaluation labels: row correct and usable as is (spec 12.2, 14)."""

from __future__ import annotations

from typing import Any

from rfq_agent.eval.labels import label_rows
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import ResolvedRow, RowFailure
from tests.builders import cost, line, msrp, oe, retail, truth
from tests.builders import quote as make_quote


def rows_of(**paths: Any) -> list[ResolvedRow]:
    outcomes = {"oe": oe(1), "msrp": msrp(1), "retail": retail(1), "cost": cost(1), **paths}
    return resolve_rows(
        [line(1)], [outcomes["oe"]], [outcomes["msrp"]], [outcomes["retail"]], [outcomes["cost"]]
    )


def test_a_fully_correct_resolved_row_is_usable() -> None:
    [label] = label_rows(rows_of(), [truth(1)])
    assert (label.row_id, label.row_correct, label.usable) == (1, True, True)


def test_wrong_msrp_makes_the_row_incorrect_and_unusable() -> None:
    [label] = label_rows(rows_of(msrp=msrp(1, 120.0)), [truth(1)])
    assert (label.row_correct, label.usable) == (False, False)


def test_correct_not_found_is_correct_but_not_usable() -> None:
    rows = rows_of(oe=oe(1, None), msrp=msrp(1, None, oe_part_no=None), retail=retail(1, {}))
    expected = truth(
        1,
        oe_status_expected="not_found",
        primary_oe=None,
        accepted_oes=[],
        supersession_chain=[],
        external_spec_available=False,
    )
    expected.msrp.status, expected.msrp.value, expected.msrp.source_oe = "not_found", None, None
    expected.retail.sites = {}
    expected.retail.expected_selected_site = None
    [label] = label_rows(rows, [expected])
    assert (label.row_correct, label.usable) == (True, False)


def test_all_outliers_is_not_usable_even_when_correct() -> None:
    quotes = [make_quote("retail_1", 80.0)]
    all_out = retail(1, {"retail_1": 80.0}, quotes=quotes, selected=None, outliers=["retail_1"])
    expected = truth(1)
    expected.retail.sites = {"retail_1": expected.retail.sites["retail_1"]}
    expected.retail.outlier_sites = ["retail_1"]
    expected.retail.expected_selected_site = None
    [label] = label_rows(rows_of(retail=all_out), [expected])
    assert (label.row_correct, label.usable) == (True, False)


def test_insufficient_data_can_still_be_usable() -> None:
    lacking = cost(
        1,
        status="insufficient_data",
        spec_source="missing",
        breakdown=None,
        material=None,
        weight_kg=None,
    )
    expected = truth(1, external_spec_available=False)
    expected.internal_spec.material = None
    expected.internal_spec.weight_kg = None
    [label] = label_rows(rows_of(cost=lacking), [expected])
    assert (label.row_correct, label.usable) == (True, True)


def test_a_failed_stage_is_incorrect() -> None:
    failed = RowFailure(row_id=1, stage="search_sites", error="timeout")
    [label] = label_rows(rows_of(retail=failed), [truth(1)])
    assert (label.row_correct, label.usable) == (False, False)
