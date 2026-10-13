"""Error injection (spec 14 experiment): which rows, and how each kind of error is made."""

from __future__ import annotations

from typing import Any

import pytest

from rfq_agent.eval.inject import InjectionRefused, inject_errors, neighbour_part_no
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import MSRPResult, OEResolution, ResolvedRow, RetailResult, RowFailure
from rfq_agent.store.runs import RowOutcomes
from tests.builders import cost, line, msrp, oe, retail


def outcome(row_id: int, **paths: Any) -> RowOutcomes:
    """An injectable row unless ``paths`` say otherwise: own OE number, three quotes."""
    fields: dict[str, Any] = {
        "row_id": row_id,
        "oe": oe(row_id, f"OE-{row_id}7"),
        "msrp": msrp(row_id),
        "retail": retail(row_id),
        "should_cost": cost(row_id),
        **paths,
    }
    return RowOutcomes(**fields)


def resolved(outcomes: list[RowOutcomes]) -> list[ResolvedRow]:
    return resolve_rows(
        [line(o.row_id) for o in outcomes],
        [o.oe for o in outcomes],
        [o.msrp for o in outcomes],
        [o.retail for o in outcomes],
        [o.should_cost for o in outcomes],
    )


def run(
    outcomes: list[RowOutcomes], n: int, seed: int = 7
) -> tuple[list[RowOutcomes], dict[int, str]]:
    changed, injections = inject_errors(resolved(outcomes), outcomes, n, seed)
    return changed, dict(injections)


def test_the_neighbour_part_number_bumps_the_last_digit() -> None:
    assert neighbour_part_no("1328.97.109") == "1328.97.100"
    assert neighbour_part_no("AB-12C") == "AB-13C"
    assert neighbour_part_no("XYZ") == "XYZ1"


def test_rows_are_drawn_reproducibly_and_kinds_rotate() -> None:
    outcomes = [outcome(i) for i in range(1, 13)]
    _, first = run(outcomes, 6)
    _, again = run(outcomes, 6)
    _, other = run(outcomes, 6, seed=8)
    assert first == again and first != other
    assert len(first) == 6
    assert sorted(first.values()) == ["msrp", "msrp", "oe", "oe", "retail", "retail"]


def test_only_injectable_rows_are_chosen() -> None:
    multiple = oe(2, status="multiple", current_oe=None, supersession_chain=[])
    outcomes = [
        outcome(1),
        outcome(2, oe=multiple, msrp=msrp(2, None), retail=retail(2, {})),  # red gate
        outcome(3, oe=oe(3, "SHARED-1")),  # shares its OE with row 4
        outcome(4, oe=oe(4, "SHARED-1")),
        outcome(5, msrp=msrp(5, None)),  # no MSRP quote
        outcome(6, retail=retail(6, {"retail_1": 80.0})),  # no other quote to switch to
        outcome(7, retail=retail(7, outliers=["retail_2", "retail_3"])),  # others are outliers
        outcome(8, should_cost=RowFailure(row_id=8, stage="extract_spec", error="x")),
        outcome(9),
    ]
    _, injected = run(outcomes, 2)
    assert set(injected) == {1, 9}


def test_too_few_injectable_rows_is_refused() -> None:
    with pytest.raises(InjectionRefused, match="2 injectable rows"):
        run([outcome(1), outcome(2)], 3)


def _injected(kind: str) -> tuple[RowOutcomes, RowOutcomes]:
    outcomes = [outcome(i) for i in range(1, 7)]
    changed, injected = run(outcomes, 6)
    row_id = next(rid for rid, k in injected.items() if k == kind)
    before = next(o for o in outcomes if o.row_id == row_id)
    after = next(o for o in changed if o.row_id == row_id)
    return before, after


def test_an_oe_error_names_a_neighbour_number_but_keeps_the_evidence() -> None:
    before, after = _injected("oe")
    assert isinstance(before.oe, OEResolution) and isinstance(after.oe, OEResolution)
    wrong = neighbour_part_no(before.oe.current_oe or "")
    assert after.oe.current_oe == wrong
    assert after.oe.supersession_chain == [*before.oe.supersession_chain[:-1], wrong]
    assert after.oe.candidates == before.oe.candidates
    assert (after.msrp, after.retail, after.should_cost) == (
        before.msrp,
        before.retail,
        before.should_cost,
    )


def test_an_msrp_error_raises_the_price_15_to_35_percent_but_keeps_the_evidence() -> None:
    before, after = _injected("msrp")
    assert isinstance(before.msrp, MSRPResult) and isinstance(after.msrp, MSRPResult)
    assert before.msrp.quote is not None and after.msrp.quote is not None
    ratio = (after.msrp.quote.effective_price or 0) / (before.msrp.quote.effective_price or 1)
    assert 1.15 <= ratio <= 1.35
    assert after.msrp.quote.list_price == after.msrp.quote.effective_price
    assert after.msrp.quote.evidence == before.msrp.quote.evidence
    assert after.oe == before.oe and after.retail == before.retail


def test_a_retail_error_selects_another_non_outlier_site() -> None:
    before, after = _injected("retail")
    assert isinstance(before.retail, RetailResult) and isinstance(after.retail, RetailResult)
    assert before.retail.selected is not None and after.retail.selected is not None
    assert after.retail.selected.source != before.retail.selected.source
    assert after.retail.selected.source not in after.retail.outliers
    assert after.retail.selected in after.retail.quotes
    assert after.oe == before.oe and after.msrp == before.msrp


def test_only_the_rows_made_wrong_are_returned() -> None:
    outcomes = [outcome(i) for i in range(1, 7)]
    changed, injected = run(outcomes, 2)
    assert sorted(o.row_id for o in changed) == sorted(injected)
    by_id = {o.row_id: o for o in outcomes}
    assert all(o != by_id[o.row_id] for o in changed)
