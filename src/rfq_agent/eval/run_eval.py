"""Evaluate a stored run against ground truth (spec 14): what ``rfq eval`` runs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from rfq_agent.config import resolve_path
from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.eval.metrics import TruthMismatch, evaluate, usage_eval
from rfq_agent.output.summary import run_summary
from rfq_agent.schemas import AgentUsage, ResolvedRow, RunEval, RunSpec
from rfq_agent.store.runs import RunStore

# The API stores an upload as ``<12 hex chars>_<original name>`` (api/app.py).
_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{12}_")


class EvalRefused(ValueError):
    """The run cannot be evaluated (not finalized, no ground truth, or the wrong one)."""


def find_truth(spec: RunSpec) -> Path | None:
    """The ground truth that belongs to the run's RFQ, or None when it cannot be told.

    A sample workbook ``rfq_sample_*`` has ``<dataset>/<stem>_ground_truth.jsonl``; a
    workbook named like one in the dataset directory (the dataset's own RFQ) has
    ``<dataset>/ground_truth.jsonl``. An API upload is judged by its original name.
    """
    dataset = resolve_path(Path(spec.dataset_dir))
    name = _UPLOAD_PREFIX.sub("", Path(spec.input).name)
    if name.startswith("rfq_sample_"):
        candidate = dataset / f"{Path(name).stem}_ground_truth.jsonl"
    elif (dataset / name).is_file():  # the dataset's own workbook, or an upload of it
        candidate = dataset / "ground_truth.jsonl"
    else:
        return None
    return candidate if candidate.is_file() else None


def load_truth(path: Path) -> list[GroundTruthRow]:
    with path.open(encoding="utf-8") as fh:
        return [GroundTruthRow.model_validate_json(line) for line in fh if line.strip()]


def pre_review_rows(store: RunStore, run_id: str) -> tuple[list[ResolvedRow], int]:
    """The system's own rows: a reviewed row as it was before its first review decision."""
    first: dict[int, ResolvedRow] = {}
    for entry in store.reviews(run_id):  # oldest first
        first.setdefault(entry.row_id, entry.snapshot)
    rows = [first.get(r.line.row_id, r) for r in store.rows(run_id)]
    return rows, len(first)


def lineage_usage(store: RunStore, run_id: str) -> list[AgentUsage] | None:
    """LLM calls of every run this one was copied from (rescore, inject), oldest last.

    None for a run that was not copied: its own calls are all it cost.
    """
    usage: list[AgentUsage] = []
    source = store.run(run_id).spec.rescored_from
    if source is None:
        return None
    while source is not None:
        usage += store.agent_usage(source)
        source = store.run(source).spec.rescored_from
    return usage


def evaluate_run(store: RunStore, run_id: str, truth_path: Path | None = None) -> RunEval:
    """Evaluate a finalized run, store the result and return it."""
    record = store.run(run_id)
    if record.status == "running":
        raise EvalRefused(f"run {run_id} is not finalized yet; evaluate it once it is")
    path = truth_path or find_truth(record.spec)
    if path is None:
        raise EvalRefused(
            f"cannot tell which ground truth belongs to {record.spec.input}; pass --truth PATH"
        )
    rows, reviewed = pre_review_rows(store, run_id)
    summary = run_summary(store, run_id)
    usage = usage_eval(
        store.agent_usage(run_id),
        store.llm_models(run_id),
        rows=len(rows),
        elapsed_s=summary.elapsed_s if summary else None,
        source=lineage_usage(store, run_id),
    )
    try:
        result = evaluate(
            rows,
            load_truth(path),
            run_id=run_id,
            strategy=record.spec.strategy,
            truth_path=str(path),
            reviewed_rows=reviewed,
            usage=usage,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            injections=store.injections(run_id),
        )
    except TruthMismatch as exc:
        raise EvalRefused(f"{exc} ({path})") from exc
    store.save_evaluation(result)
    return result


def stored_evaluations(store: RunStore, run_ids: Sequence[str]) -> list[RunEval]:
    """Evaluations of runs to compare: all evaluated, on the same truth file and rows."""
    evals = []
    for run_id in run_ids:
        found = store.evaluation(run_id)
        if found is None:
            raise EvalRefused(f"run {run_id} has no evaluation; run: rfq eval --run {run_id}")
        evals.append(found)
    first = evals[0]
    for other in evals[1:]:
        same_rows = {r.row_id for r in other.row_evals} == {r.row_id for r in first.row_evals}
        if other.truth_path != first.truth_path or not same_rows:
            raise EvalRefused(
                f"runs {first.run_id} and {other.run_id} were not evaluated on the same RFQ "
                "rows; compare runs of the same RFQ"
            )
    return evals
