"""Review decisions on the worksheet (spec 13, phase-7 Q12-Q14)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rfq_agent.orchestrator.review import (
    MANUAL_PRICE_REASON,
    apply_reviews,
    manual_oe_resolution,
)
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import ResolvedRow, ReviewEntry, ReviewInput, RowFailure
from tests.builders import cost, line, msrp, oe, retail


def _row() -> ResolvedRow:
    return resolve_rows([line(1)], [oe(1)], [msrp(1, 100.0)], [retail(1)], [cost(1)])[0]


def _entry(action: str, **kw: object) -> ReviewEntry:
    return ReviewEntry.model_validate({"row_id": 1, "action": action, "snapshot": _row(), **kw})


def test_accept_and_reject_only_record_the_outcome() -> None:
    row = _row()

    accepted = apply_reviews(row, [_entry("accept")])
    rejected = apply_reviews(row, [_entry("reject")])

    assert accepted.worksheet.review_status == "accepted"
    assert rejected.worksheet.review_status == "rejected"
    for shown in (accepted, rejected):
        assert shown.worksheet.model_dump(exclude={"review_status"}) == row.worksheet.model_dump(
            exclude={"review_status"}
        )


def test_a_manual_msrp_overrides_the_worksheet_but_not_the_agent_result() -> None:
    row = _row()

    shown = apply_reviews(row, [_entry("modify_price", msrp_before=100.0, msrp_after=123.5)])

    assert (shown.worksheet.msrp, shown.worksheet.msrp_status) == (123.5, "found")
    assert shown.worksheet.selected_retail_price == row.worksheet.selected_retail_price
    assert shown.worksheet.review_status == "modified"
    assert shown.worksheet.review_reasons[-1] == MANUAL_PRICE_REASON
    assert shown.msrp == row.msrp


def test_a_manual_retail_price_becomes_the_reference_retail_price() -> None:
    shown = apply_reviews(_row(), [_entry("modify_price", retail_after=88.0)])

    assert shown.worksheet.selected_retail_price == 88.0
    assert shown.worksheet.selected_retail_source == "manual_review"
    assert shown.worksheet.msrp == 100.0


def test_a_manual_oe_is_a_resolved_oe_with_the_known_part_number() -> None:
    original = oe(4, None, status="multiple", our_part_no="OP-4", our_part_source="given")

    manual = manual_oe_resolution(original, " new-77 ", run_id="run-x")

    assert (manual.row_id, manual.status, manual.current_oe) == (4, "resolved", "NEW-77")
    assert manual.supersession_chain == ["NEW-77"]
    assert (manual.our_part_no, manual.our_part_source) == ("OP-4", "given")
    [candidate] = manual.candidates
    assert candidate.match_type == "manual"
    assert candidate.evidence[0].source == "manual_review"


def test_a_manual_oe_after_a_failed_oe_has_no_part_number() -> None:
    manual = manual_oe_resolution(RowFailure(row_id=2, stage="search", error="x"), "A1", "r")

    assert (manual.our_part_no, manual.our_part_source) == (None, "none")


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "modify_oe"},
        {"action": "modify_oe", "oe": "  "},
        {"action": "modify_price"},
        {"action": "modify_price", "msrp": 0},
        {"action": "modify_price", "retail": -5},
    ],
)
def test_incomplete_decisions_are_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReviewInput.model_validate(fields)
