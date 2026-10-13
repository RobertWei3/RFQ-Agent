"""Many-to-one price reuse in the orchestrator (spec 13, phase-7 Q2/Q4)."""

from __future__ import annotations

from rfq_agent.orchestrator.prices import expand_price_results, plan_price_jobs
from rfq_agent.schemas import RowFailure
from tests.builders import msrp, oe


def test_rows_sharing_a_current_oe_run_once_and_copy_the_leader() -> None:
    upstream = [oe(3, "NEW-3"), oe(1, "new 3"), oe(2, "OTHER-9")]

    plan = plan_price_jobs(upstream, cached={})

    assert [job.row_id for job in plan.jobs] == [1, 2]
    results = expand_price_results(plan, [msrp(1, 100.0), msrp(2, 50.0)])
    assert {rid: r.row_id for rid, r in results.items()} == {1: 1, 2: 2, 3: 3}
    assert results[3].model_dump(exclude={"row_id"}) == msrp(1, 100.0).model_dump(
        exclude={"row_id"}
    )


def test_an_earlier_batch_result_is_reused_without_running() -> None:
    upstream = [oe(7, "NEW-3"), oe(8, "OTHER-9")]

    plan = plan_price_jobs(upstream, cached={"NEW3": msrp(2, 100.0)})

    assert [job.row_id for job in plan.jobs] == [8]
    results = expand_price_results(plan, [msrp(8, 50.0)])
    assert results[7].row_id == 7
    assert results[7].quote is not None and results[7].quote.effective_price == 100.0


def test_rows_without_a_unique_oe_are_never_shared() -> None:
    unresolved = [
        oe(1, None),
        oe(2, None),
        oe(3, None, status="multiple"),
        RowFailure(row_id=4, stage="search", error="timeout"),
    ]

    plan = plan_price_jobs(unresolved, cached={})

    assert [job.row_id for job in plan.jobs] == [1, 2, 3, 4]
    assert plan.followers == {}


def test_a_failed_leader_fails_its_followers_in_the_same_batch() -> None:
    plan = plan_price_jobs([oe(1, "NEW-3"), oe(5, "NEW-3")], cached={})

    results = expand_price_results(
        plan, [RowFailure(row_id=1, stage="search_chain", error="timeout")]
    )

    assert results[5] == RowFailure(row_id=5, stage="search_chain", error="timeout")
