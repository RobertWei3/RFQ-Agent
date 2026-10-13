"""Per-row evaluation labels: "row correct" and "usable as is" (spec 12.2, 14)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rfq_agent.confidence.evidence import all_quotes_outliers
from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.eval.quick_compare import compare_worksheet
from rfq_agent.schemas import ResolvedRow


@dataclass(frozen=True)
class RowLabel:
    row_id: int
    row_correct: bool  # every stage right and no stage failed
    usable: bool  # row correct, OE resolved and a reference retail price unless no quotes


def label_rows(rows: Sequence[ResolvedRow], truth: Sequence[GroundTruthRow]) -> list[RowLabel]:
    """Both labels for each row, in row_id order."""
    by_id = {r.line.row_id: r for r in rows}
    labels = []
    for check in compare_worksheet(rows, truth):
        row = by_id[check.row_id]
        usable = (
            check.correct and row.worksheet.oe_status == "resolved" and not all_quotes_outliers(row)
        )
        labels.append(RowLabel(row_id=check.row_id, row_correct=check.correct, usable=usable))
    return labels
