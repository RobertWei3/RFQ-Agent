"""Many-to-one price reuse: one oe_msrp / retail_price run per current OE (spec 13)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar, cast

from pydantic import BaseModel

from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import OEResolution, RowFailure


class _HasRowId(Protocol):
    row_id: int


R = TypeVar("R", bound=BaseModel)
OEOutcome = OEResolution | RowFailure


def price_key(outcome: OEOutcome) -> str | None:
    """Normalized current OE of a row whose prices can be shared, else None."""
    if isinstance(outcome, OEResolution) and outcome.status == "resolved" and outcome.current_oe:
        return normalize_part_no(outcome.current_oe)
    return None


@dataclass
class PricePlan(Generic[R]):
    """Which rows run a price agent, which copy a leader, which reuse an earlier batch."""

    jobs: list[OEOutcome] = field(default_factory=list)  # upstream of every row that runs
    followers: dict[int, int] = field(default_factory=dict)  # row_id -> leader row_id
    reused: dict[int, R] = field(default_factory=dict)  # row_id -> cached result (own row_id)
    sources: dict[int, int] = field(default_factory=dict)  # reused row_id -> cached row_id

    def copied_from(self, row_id: int) -> int | None:
        """Row whose agent run produced this row's result; None when the row ran itself."""
        if row_id in self.followers:
            return self.followers[row_id]
        return self.sources.get(row_id)


def plan_price_jobs(upstream: Sequence[OEOutcome], cached: Mapping[str, R]) -> PricePlan[R]:
    """Plan one price path for a batch.

    ``cached`` maps a price key to a non-failure result from an earlier completed batch.
    Rows without a unique OE are not shared: they run (and short-circuit) on their own.
    """
    plan: PricePlan[R] = PricePlan()
    leaders: dict[str, int] = {}
    for outcome in sorted(upstream, key=lambda o: o.row_id):
        key = price_key(outcome)
        if key is not None and key in cached:
            plan.reused[outcome.row_id] = _with_row_id(cached[key], outcome.row_id)
            plan.sources[outcome.row_id] = _row_id(cached[key])
        elif key is not None and key in leaders:
            plan.followers[outcome.row_id] = leaders[key]
        else:
            if key is not None:
                leaders[key] = outcome.row_id
            plan.jobs.append(outcome)
    return plan


def expand_price_results(
    plan: PricePlan[R], results: Sequence[R | RowFailure]
) -> dict[int, R | RowFailure]:
    """Results of every row in the batch: run, copied from the leader, or reused."""
    by_row: dict[int, R | RowFailure] = {_row_id(r): r for r in results}
    expanded: dict[int, R | RowFailure] = dict(by_row)
    for row_id, leader in plan.followers.items():
        expanded[row_id] = _with_row_id(by_row[leader], row_id)
    expanded.update(plan.reused)
    return expanded


T = TypeVar("T", bound=BaseModel)


def _row_id(result: BaseModel) -> int:
    return cast(_HasRowId, result).row_id


def _with_row_id(result: T, row_id: int) -> T:
    return result.model_copy(update={"row_id": row_id}, deep=True)
