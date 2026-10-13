"""Run summary (spec 12.3): the worksheet's second sheet and ``GET /runs/{id}``."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import get_args

from rfq_agent.schemas import (
    Band,
    CoveragePath,
    MSRPResult,
    OEResolution,
    ResolvedRow,
    RetailResult,
    ReviewStatus,
    RunRecord,
    RunSummary,
    ShouldCostResult,
)
from rfq_agent.store.runs import RunStore

BANDS: tuple[Band, ...] = get_args(Band)
REVIEW_STATUSES: tuple[ReviewStatus, ...] = get_args(ReviewStatus)


def run_summary(store: RunStore, run_id: str) -> RunSummary | None:
    """Totals of a finalized run; None while it is still running (no worksheet yet)."""
    record = store.run(run_id)
    if record.status == "running":
        return None
    rows = store.rows(run_id)
    usage = store.llm_usage(run_id)
    bands = Counter(r.worksheet.band for r in rows)
    reviews = Counter(r.worksheet.review_status for r in rows)
    pending = [r for r in rows if r.worksheet.band == "red" and r.worksheet.review_status is None]
    return RunSummary(
        rows=len(rows),
        bands={band: bands[band] for band in BANDS},
        reviews={status: reviews[status] for status in REVIEW_STATUSES},
        review_pending=len(pending) if record.spec.review else 0,
        coverage=coverage(rows),
        rows_with_failure=sum(bool(r.worksheet.failures) for r in rows),
        llm_calls=usage.calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        llm_cost_usd=usage.cost_usd,
        elapsed_s=_elapsed(record),
    )


def coverage(rows: Sequence[ResolvedRow]) -> dict[CoveragePath, float]:
    """Share of rows each path covered, from the system's own results (no manual prices)."""
    covered: dict[CoveragePath, list[bool]] = {
        "oe": [isinstance(r.oe, OEResolution) and r.oe.status == "resolved" for r in rows],
        "msrp": [
            isinstance(r.msrp, MSRPResult) and r.msrp.status in ("found", "discontinued")
            for r in rows
        ],
        "retail": [
            isinstance(r.retail, RetailResult) and r.retail.selected is not None for r in rows
        ],
        "should_cost": [
            isinstance(r.should_cost, ShouldCostResult) and r.should_cost.status == "estimated"
            for r in rows
        ],
    }
    return {
        path: round(sum(hits) / len(rows), 4) if rows else 0.0 for path, hits in covered.items()
    }


def _elapsed(record: RunRecord) -> float | None:
    if record.finished_at is None:
        return None
    started = datetime.fromisoformat(record.created_at)
    return (datetime.fromisoformat(record.finished_at) - started).total_seconds()
