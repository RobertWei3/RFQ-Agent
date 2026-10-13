"""Entity resolution (spec 12.1): merge the four agent outcomes into one row per row_id."""

from __future__ import annotations

from typing import Any

import pytest

from rfq_agent.resolution import ResolutionInputError, resolve_rows
from rfq_agent.schemas import MSRPResult, OECandidate, OEResolution, RowFailure
from tests.builders import breakdown, cost, line, msrp, oe, retail


def test_one_resolved_row_becomes_one_worksheet_row() -> None:
    [row] = resolve_rows([line(1)], [oe(1)], [msrp(1)], [retail(1)], [cost(1)])

    ws = row.worksheet
    assert ws.row_id == 1
    assert ws.customer_part_no_raw == " CP-1 "
    assert ws.customer_part_no == "CP1"
    assert ws.our_part_no == "OP-1"
    assert ws.oe_status == "resolved"
    assert ws.current_oe == "NEW-3"
    assert ws.supersession_chain == ["OLD-1", "NEW-3"]
    assert ws.msrp == 100.0
    assert ws.msrp_status == "found"
    assert ws.retail_prices == {"retail_1": 80.0, "retail_2": 82.0, "retail_3": 85.0}
    assert ws.selected_retail_price == 80.0
    assert ws.selected_retail_source == "retail_1"
    assert ws.retail_outliers == []
    assert ws.should_cost_fob == 20.0
    assert ws.landed_cost == 30.0
    assert ws.shared_oe_rows == []
    assert ws.price_source_row is None
    assert ws.failures == []
    assert ws.confidence_score is None and ws.band is None
    assert ws.reasoning_summary == {
        "oe": "oe reasoning 1",
        "msrp": "msrp reasoning 1",
        "retail": "retail reasoning 1",
        "should_cost": "cost reasoning 1",
    }
    assert row.oe == oe(1)
    assert row.confidence is None


# --- many-to-one: shared-OE groups ------------------------------------------------------


def test_rows_with_the_same_current_oe_reuse_the_representative_prices() -> None:
    # Rows 1 and 3 resolve to the same OE (written differently); row 2 is unrelated.
    lines = [line(1), line(2), line(3)]
    oes = [oe(1), oe(2, current="OTHER-9"), oe(3, current="new 3")]
    msrps = [msrp(1, 100.0), msrp(2, 50.0), msrp(3, 100.0)]
    retails = [retail(1), retail(2), retail(3)]
    costs = [cost(1), cost(2), cost(3, breakdown=breakdown(fob=21.0, landed=31.0))]

    rows = resolve_rows(lines, oes, msrps, retails, costs)

    first, other, third = (r.worksheet for r in rows)
    assert first.shared_oe_rows == [3] and first.price_source_row is None
    assert third.shared_oe_rows == [1] and third.price_source_row == 1
    assert other.shared_oe_rows == [] and other.price_source_row is None
    assert rows[2].msrp == msrps[0] and rows[2].retail == retails[0]
    assert "shares OE with row 1; prices taken from row 1" in third.reasoning_summary["shared"]
    assert third.landed_cost == 31.0  # should-cost is per our part, never shared
    assert third.review_reasons == []  # identical price conclusions


def test_representative_is_chosen_per_path_skipping_failed_rows() -> None:
    lines = [line(1), line(2)]
    oes = [oe(1), oe(2)]
    failed = RowFailure(row_id=1, stage="search_sites", error="all 5 sites failed")
    rows = resolve_rows(lines, oes, [msrp(1), msrp(2)], [failed, retail(2)], [cost(1), cost(2)])

    assert rows[0].msrp == msrp(1) and rows[1].msrp == msrp(1)
    assert rows[0].retail == retail(2) and rows[1].retail == retail(2)
    assert rows[0].worksheet.selected_retail_source == "retail_1"
    assert rows[0].worksheet.failures == []


def test_differing_price_conclusions_in_a_group_are_flagged_for_review() -> None:
    lines = [line(1), line(2)]
    oes = [oe(1), oe(2)]
    msrps = [msrp(1, 100.0), msrp(2, 120.0)]
    retails = [retail(1), retail(2, {"retail_2": 82.0, "retail_3": 85.0})]

    rows = resolve_rows(lines, oes, msrps, retails, [cost(1), cost(2)])

    for row in rows:
        assert row.worksheet.msrp == 100.0
        assert row.worksheet.selected_retail_source == "retail_1"
        assert "shared-OE rows disagree on prices (rows 1, 2)" in row.worksheet.review_reasons


def test_multiple_rows_are_never_grouped() -> None:
    cands = [
        OECandidate(oe_part_no=pn, brand=None, match_type="interchange", evidence=[])
        for pn in ("A-1", "B-2")
    ]
    multi: dict[str, Any] = {
        "status": "multiple",
        "current_oe": None,
        "supersession_chain": [],
        "candidates": cands,
    }
    rows = resolve_rows(
        [line(1), line(2)],
        [oe(1, **multi), oe(2, **multi)],
        [msrp(1, None), msrp(2, None)],
        [retail(1, {}), retail(2, {})],
        [cost(1), cost(2)],
    )
    assert all(r.worksheet.shared_oe_rows == [] for r in rows)
    assert rows[0].worksheet.oe_candidates == ["A-1", "B-2"]
    assert "OE not uniquely resolved; pending manual selection" in rows[0].worksheet.review_reasons


# --- failures and review reasons --------------------------------------------------------


def test_failed_path_leaves_its_columns_empty_and_is_listed() -> None:
    failed = RowFailure(row_id=1, stage="search_chain", error="timeout")
    [row] = resolve_rows([line(1)], [oe(1)], [failed], [retail(1)], [cost(1)])

    ws = row.worksheet
    assert ws.msrp is None and ws.msrp_status is None
    assert ws.failures == ["msrp:search_chain:timeout"]
    assert "MSRP not retrieved: timeout" in ws.review_reasons
    assert ws.reasoning_summary["msrp"] == "timeout"
    assert ws.selected_retail_source == "retail_1"


def test_oe_failure_is_reported_once_as_the_root_cause() -> None:
    root = RowFailure(row_id=1, stage="extract_candidates", error="bad json")
    upstream = {"stage": "upstream_oe", "error": "oe_interchange failed"}
    [row] = resolve_rows(
        [line(1)],
        [root],
        [RowFailure(row_id=1, **upstream)],
        [RowFailure(row_id=1, **upstream)],
        [RowFailure(row_id=1, **upstream)],
    )
    ws = row.worksheet
    assert ws.oe_status is None and ws.current_oe is None
    assert ws.failures == ["oe:extract_candidates:bad json"]
    assert ws.review_reasons == ["OE resolution failed: bad json"]


def test_found_nothing_is_worded_apart_from_failures() -> None:
    insufficient = cost(1, status="insufficient_data", spec_source="missing", breakdown=None)
    [row] = resolve_rows([line(1)], [oe(1)], [msrp(1, None)], [retail(1, {})], [insufficient])
    assert row.worksheet.failures == []
    assert row.worksheet.review_reasons == [
        "no MSRP found",
        "no retail quotes found",
        "should-cost: insufficient data",
    ]


def test_price_and_spec_caveats_become_review_reasons() -> None:
    fallback = msrp(1, status="discontinued")
    assert fallback.quote is not None
    fallback.quote.matched_part_no = "OLD-1"
    outlier = retail(1, {"retail_1": 80.0, "retail_2": 82.0, "retail_3": 200.0})
    outlier.outliers = ["retail_3"]
    [row] = resolve_rows(
        [line(1)], [oe(1)], [fallback], [outlier], [cost(1, spec_source="conflict")]
    )
    assert row.worksheet.review_reasons == [
        "MSRP page marks the part discontinued",
        "MSRP taken from superseded number OLD-1",
        "retail outliers: retail_3",
        "internal and external specs conflict (internal values used)",
    ]


def test_all_quotes_outliers_means_no_retail_reference() -> None:
    every = retail(1, {"retail_1": 10.0, "retail_2": 90.0}, selected=None)
    every.outliers = ["retail_1", "retail_2"]
    [row] = resolve_rows([line(1)], [oe(1)], [msrp(1)], [every], [cost(1)])
    assert "no reliable retail reference: every quote is an outlier" in row.worksheet.review_reasons


def test_oe_not_found_reports_only_the_root_reason() -> None:
    [row] = resolve_rows(
        [line(1)], [oe(1, current=None)], [msrp(1, None)], [retail(1, {})], [cost(1)]
    )
    assert row.worksheet.review_reasons == ["no OE number found"]


def test_rows_must_match_across_inputs() -> None:
    with pytest.raises(ResolutionInputError, match=r"msrp: missing \[2\]; extra \[9\]"):
        resolve_rows(
            [line(1), line(2)],
            [oe(1), oe(2)],
            [msrp(1), msrp(9)],
            [retail(1), retail(2)],
            [cost(1), cost(2)],
        )


# --- review fixes ------------------------------------------------------------------------


def multiple_oe(row_id: int, extra: list[OECandidate] | None = None) -> Any:
    cands = [
        OECandidate(oe_part_no=pn, brand=None, match_type="interchange", evidence=[])
        for pn in ("A-1", "B-2")
    ]
    return oe(
        row_id,
        status="multiple",
        current_oe=None,
        supersession_chain=[],
        candidates=cands + (extra or []),
    )


def test_unpriced_rows_show_no_msrp_status() -> None:
    rows = resolve_rows(
        [line(1), line(2)],
        [multiple_oe(1), oe(2, current=None)],
        [msrp(1, None), msrp(2, None)],
        [retail(1, {}), retail(2, {})],
        [cost(1), cost(2)],
    )
    for row in rows:
        assert row.worksheet.msrp_status is None
        assert isinstance(row.msrp, MSRPResult) and row.msrp.status == "not_found"


def test_inferred_candidates_stay_off_the_worksheet() -> None:
    inferred = OECandidate(oe_part_no="GUESS-9", brand=None, match_type="llm_inferred", evidence=[])
    [row] = resolve_rows(
        [line(1)], [multiple_oe(1, [inferred])], [msrp(1, None)], [retail(1, {})], [cost(1)]
    )
    assert row.worksheet.oe_candidates == ["A-1", "B-2"]
    assert isinstance(row.oe, OEResolution) and len(row.oe.candidates) == 3


def test_row_zero_can_be_the_representative() -> None:
    rows = resolve_rows(
        [line(0), line(1)],
        [oe(0), oe(1)],
        [msrp(0, 100.0), msrp(1, 100.0)],
        [retail(0), retail(1)],
        [cost(0), cost(1)],
    )
    assert rows[1].worksheet.price_source_row == 0
    assert rows[1].msrp == msrp(0, 100.0)
    assert rows[0].worksheet.price_source_row is None


def test_paths_with_different_representatives_are_both_named() -> None:
    failed = RowFailure(row_id=1, stage="search_sites", error="all 5 sites failed")
    rows = resolve_rows(
        [line(1), line(2), line(3)],
        [oe(1), oe(2), oe(3)],
        [msrp(1), msrp(2), msrp(3)],
        [failed, retail(2), retail(3)],
        [cost(1), cost(2), cost(3)],
    )
    first, _, third = (r.worksheet for r in rows)
    assert third.price_source_row == 1  # the MSRP representative
    assert third.reasoning_summary["shared"] == (
        "shares OE with row 1, 2; MSRP taken from row 1, retail taken from row 2"
    )
    assert first.price_source_row == 2  # own MSRP, reused retail


def _group(msrps: list[Any], retails: list[Any]) -> list[Any]:
    return resolve_rows([line(1), line(2)], [oe(1), oe(2)], msrps, retails, [cost(1), cost(2)])


def test_different_selected_site_alone_is_a_disagreement() -> None:
    other = retail(2)
    other.selected = other.quotes[1]
    rows = _group([msrp(1), msrp(2)], [retail(1), other])
    assert rows[0].worksheet.shared_prices_disagree


def test_different_msrp_status_alone_is_a_disagreement() -> None:
    rows = _group([msrp(1), msrp(2, status="discontinued")], [retail(1), retail(2)])
    assert rows[0].worksheet.shared_prices_disagree


def test_a_failed_member_is_not_a_disagreement() -> None:
    failed = RowFailure(row_id=2, stage="search_chain", error="timeout")
    rows = _group([msrp(1), failed], [retail(1), retail(2)])
    assert not rows[0].worksheet.shared_prices_disagree


def test_missing_retail_reference_in_one_member_is_a_disagreement() -> None:
    rows = _group([msrp(1), msrp(2)], [retail(1), retail(2, selected=None)])
    assert rows[0].worksheet.shared_prices_disagree
