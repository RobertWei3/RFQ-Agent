"""Run progress, summary and row pages (spec 12.3, 15.1; phase-8 Q15, Q18)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rfq_agent.output.summary import run_summary
from rfq_agent.schemas import ResolvedRow, ReviewEntry, RowFailure, RunSpec
from rfq_agent.store.db import connect
from rfq_agent.store.runs import RunStore, StoredRow
from tests.builders import cost, line, msrp, oe, resolved_row, retail


def _spec(review: bool = True) -> RunSpec:
    return RunSpec(
        input="rfq.xlsx", dataset_dir="x", batch_size=2, strategy="evidence", review=review
    )


def _reviewed(row: ResolvedRow, status: str, **sheet: Any) -> ResolvedRow:
    update = {"review_status": status, **sheet}
    return row.model_copy(update={"worksheet": row.worksheet.model_copy(update=update)})


def _store_with_rows(
    tmp_path: Path, rows: list[ResolvedRow], review: bool = True
) -> tuple[RunStore, str]:
    store = RunStore.open(tmp_path / "rfq.db")
    run_id = store.create_run(_spec(review), [r.line for r in rows])
    store.save_rows(run_id, [StoredRow(row=r, basis="b") for r in rows])
    return store, run_id


def _five_rows() -> list[ResolvedRow]:
    no_oe = oe(3, None)
    return [
        resolved_row(1, "green"),
        resolved_row(2, "yellow", msrp=msrp(2, None), retail=retail(2, {})),
        _reviewed(
            resolved_row(
                3,
                "red",
                oe=no_oe,
                msrp=msrp(3, None, oe_part_no=None),
                retail=retail(3, {}, oe_part_no=None),
                should_cost=cost(3, status="insufficient_data", breakdown=None),
            ),
            "rejected",
        ),
        # A manual MSRP shows on the worksheet but is not system coverage.
        _reviewed(
            resolved_row(
                4,
                "red",
                msrp=msrp(4, None),
                should_cost=RowFailure(row_id=4, stage="internal_spec", error="down"),
            ),
            "modified",
            msrp=42.0,
            msrp_status="found",
        ),
        resolved_row(5, "red"),
    ]


def _log_calls(db: Path, run_id: str, calls: list[tuple[int, int, float | None]]) -> None:
    conn = connect(db)
    with conn:
        conn.executemany(
            "INSERT INTO llm_calls (ts, rfq_run_id, input_tokens, output_tokens, cost_usd, status) "
            "VALUES ('t', ?, ?, ?, ?, 'ok')",
            [(run_id, i, o, c) for i, o, c in calls],
        )
    conn.close()


def test_progress_counts_batches_by_status(tmp_path: Path) -> None:
    store = RunStore.open(tmp_path / "rfq.db")
    run_id = store.create_run(_spec(), [line(i) for i in range(1, 6)])
    store.save_batch(run_id, 1, [])
    store.fail_batch(run_id, 2, "boom")

    progress = store.progress(run_id)

    assert (progress.batches_total, progress.pending, progress.running) == (3, 1, 0)
    assert (progress.completed, progress.failed) == (1, 1)


def test_summary_counts_bands_reviews_coverage_and_llm_usage(tmp_path: Path) -> None:
    store, run_id = _store_with_rows(tmp_path, _five_rows())
    _log_calls(tmp_path / "rfq.db", run_id, [(100, 10, 0.01), (200, 20, 0.02)])
    _log_calls(tmp_path / "rfq.db", "other-run", [(999, 999, 9.0)])
    store.mark_finalized(run_id, "awaiting_review")

    summary = run_summary(store, run_id)

    assert summary is not None
    assert summary.rows == 5
    assert summary.bands == {"green": 1, "yellow": 1, "red": 3}
    assert summary.reviews == {"accepted": 0, "modified": 1, "rejected": 1}
    assert summary.review_pending == 1
    assert summary.coverage == {"oe": 0.8, "msrp": 0.4, "retail": 0.6, "should_cost": 0.6}
    assert summary.rows_with_failure == 1
    assert (summary.llm_calls, summary.input_tokens, summary.output_tokens) == (2, 300, 30)
    assert summary.llm_cost_usd is not None and abs(summary.llm_cost_usd - 0.03) < 1e-9
    assert summary.elapsed_s is not None and summary.elapsed_s >= 0


def test_summary_is_empty_until_the_run_is_finalized_and_has_no_queue_without_review(
    tmp_path: Path,
) -> None:
    store, run_id = _store_with_rows(tmp_path, _five_rows(), review=False)
    _log_calls(tmp_path / "rfq.db", run_id, [(1, 1, None)])

    assert run_summary(store, run_id) is None

    store.mark_finalized(run_id, "completed")
    summary = run_summary(store, run_id)
    assert summary is not None
    assert summary.review_pending == 0
    assert summary.llm_cost_usd is None  # a call without a price


def test_row_pages_filter_by_band_and_review_status_in_row_order(tmp_path: Path) -> None:
    store, run_id = _store_with_rows(tmp_path, _five_rows())

    red_open = store.row_page(run_id, bands=["red"], statuses=["unreviewed"], offset=0, limit=10)
    reviewed = store.row_page(
        run_id, bands=[], statuses=["rejected", "modified"], offset=0, limit=10
    )
    second_page = store.row_page(run_id, bands=[], statuses=[], offset=2, limit=2)

    assert [r.row_id for r in red_open.items] == [5]
    assert [r.row_id for r in reviewed.items] == [3, 4]
    assert reviewed.items[1].msrp == 42.0  # worksheet columns as shown, overlays included
    assert (second_page.total, second_page.offset, second_page.limit) == (5, 2, 2)
    assert [r.row_id for r in second_page.items] == [3, 4]


def test_review_entries_are_listed_per_row(tmp_path: Path) -> None:
    rows = _five_rows()
    store, run_id = _store_with_rows(tmp_path, rows)
    store.log_review(run_id, ReviewEntry(row_id=3, action="reject", snapshot=rows[2]))
    store.log_review(run_id, ReviewEntry(row_id=5, action="accept", snapshot=rows[4]))

    assert [e.row_id for e in store.reviews(run_id, row_id=3)] == [3]
