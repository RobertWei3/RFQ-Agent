"""Evaluation metrics (spec 14), as pure functions over scores, labels and stage outcomes.

Calibration treats ``score / 100`` as the probability that a row is usable as is.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import get_args

from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.eval.labels import RowLabel, label_rows
from rfq_agent.eval.quick_compare import (
    compare,
    compare_msrp,
    compare_retail,
    compare_should_cost,
    own_result,
)
from rfq_agent.output.summary import coverage
from rfq_agent.partno import normalize_part_no
from rfq_agent.schemas import (
    AgentUsage,
    Band,
    BandEval,
    CalibrationEval,
    CurvePoint,
    InjectionEval,
    InjectionKind,
    MSRPEval,
    MSRPResult,
    OEEval,
    OEResolution,
    ReliabilityBin,
    ResolvedRow,
    RetailEval,
    RetailResult,
    RowEval,
    RunEval,
    ShouldCostEval,
    SiteEval,
    StrategyName,
    ThresholdPick,
    UsageEval,
)

BIN_WIDTH = 10
N_BINS = 10
THRESHOLD_TARGETS = (0.90, 0.95, 0.99)


def share(hits: int, total: int) -> float | None:
    """hits / total, or None when there is nothing to divide by."""
    return hits / total if total else None


def _bin_of(score: int) -> int:
    return min(score // BIN_WIDTH, N_BINS - 1)


def reliability(scores: Sequence[int], labels: Sequence[bool]) -> list[ReliabilityBin]:
    """Ten equal-width bins [0, 10) ... [90, 100]: mean score and usable rate in each."""
    members: list[list[tuple[int, bool]]] = [[] for _ in range(N_BINS)]
    for score, label in zip(scores, labels, strict=True):
        members[_bin_of(score)].append((score, label))
    bins = []
    for i, rows in enumerate(members):
        bins.append(
            ReliabilityBin(
                lo=i * BIN_WIDTH,
                hi=(i + 1) * BIN_WIDTH,
                rows=len(rows),
                mean_score=statistics.fmean(s / 100 for s, _ in rows) if rows else None,
                usable_rate=share(sum(label for _, label in rows), len(rows)),
            )
        )
    return bins


def ece(scores: Sequence[int], labels: Sequence[bool]) -> float:
    """Expected calibration error: bin gaps |mean score - usable rate| weighted by bin size."""
    total = len(scores)
    return sum(
        b.rows / total * abs(b.mean_score - b.usable_rate)
        for b in reliability(scores, labels)
        if b.mean_score is not None and b.usable_rate is not None
    )


def auroc(scores: Sequence[int], labels: Sequence[bool]) -> float | None:
    """Area under the ROC curve via the Mann-Whitney rank sum (ties share the mean rank)."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and scores[order[end + 1]] == scores[order[start]]:
            end += 1
        mean_rank = (start + end) / 2 + 1  # ranks are 1-based
        for k in range(start, end + 1):
            ranks[order[k]] = mean_rank
        start = end + 1
    rank_sum = sum(r for r, label in zip(ranks, labels, strict=True) if label)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def coverage_curve(scores: Sequence[int], labels: Sequence[bool]) -> list[CurvePoint]:
    """For each threshold 0-100: share of rows at or above it, and how many are usable."""
    total = len(scores)
    points = []
    for threshold in range(101):
        kept = [label for score, label in zip(scores, labels, strict=True) if score >= threshold]
        points.append(
            CurvePoint(
                threshold=threshold,
                coverage=len(kept) / total if total else 0.0,
                accuracy=share(sum(kept), len(kept)),
            )
        )
    return points


def threshold_picks(
    curve: Sequence[CurvePoint], targets: Sequence[float] = THRESHOLD_TARGETS
) -> list[ThresholdPick]:
    """The lowest threshold whose accuracy reaches each target, with its coverage."""
    picks = []
    for target in targets:
        hit = next((p for p in curve if p.accuracy is not None and p.accuracy >= target), None)
        picks.append(
            ThresholdPick(
                target=target,
                threshold=hit.threshold if hit else None,
                coverage=hit.coverage if hit else None,
            )
        )
    return picks


# --- per-stage metrics ------------------------------------------------------------------


def _by_id(truth: Sequence[GroundTruthRow]) -> dict[int, GroundTruthRow]:
    return {t.row_id: t for t in truth}


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _norm(values: Sequence[str]) -> list[str]:
    return [normalize_part_no(v) for v in values]


def oe_eval(rows: Sequence[ResolvedRow], truth: Sequence[GroundTruthRow]) -> OEEval:
    """OE coverage, accuracy of resolved rows, conclusion accuracy and exact chains."""
    by_id = _by_id(truth)
    resolved = [
        (r.oe, by_id[r.line.row_id])
        for r in rows
        if isinstance(r.oe, OEResolution) and r.oe.status == "resolved"
    ]
    right = [
        t.primary_oe is not None
        and normalize_part_no(o.current_oe or "") == normalize_part_no(t.primary_oe)
        for o, t in resolved
    ]
    both = [(o, t) for o, t in resolved if t.oe_status_expected == "resolved"]
    chains = [_norm(o.supersession_chain) == _norm(t.supersession_chain) for o, t in both]
    checks = compare([r.oe for r in rows], truth)
    return OEEval(
        coverage=coverage(rows)["oe"],
        accuracy=share(sum(right), len(right)),
        conclusion_accuracy=sum(c.oe_ok and not c.failed for c in checks) / len(checks),
        chain_accuracy=share(sum(chains), len(chains)),
    )


def msrp_eval(rows: Sequence[ResolvedRow], truth: Sequence[GroundTruthRow]) -> MSRPEval:
    """MSRP coverage, price accuracy (1%) of priced rows, status accuracy, mean error."""
    by_id = _by_id(truth)
    checks = compare_msrp([own_result(r.msrp, r) for r in rows], truth)
    priced = [c for c in checks if c.price_ok is not None]
    errors = []
    for r in rows:
        want = by_id[r.line.row_id].msrp.value
        if isinstance(r.msrp, MSRPResult) and r.msrp.quote is not None and want:
            got = r.msrp.quote.effective_price
            if got is not None:
                errors.append(abs(got - want) / want)
    return MSRPEval(
        coverage=coverage(rows)["msrp"],
        accuracy=share(sum(bool(c.price_ok) for c in priced), len(priced)),
        status_accuracy=sum(c.status_ok and not c.failed for c in checks) / len(checks),
        mean_rel_error=_mean(errors),
    )


def retail_eval(rows: Sequence[ResolvedRow], truth: Sequence[GroundTruthRow]) -> RetailEval:
    """Per-site coverage, reference-site accuracy and outlier precision / recall."""
    by_id = _by_id(truth)
    outcomes = [own_result(r.retail, r) for r in rows]
    quoted: dict[int, set[str]] = {
        o.row_id: {q.source for q in o.quotes} if isinstance(o, RetailResult) else set()
        for o in outcomes
    }
    listed: dict[int, set[str]] = {
        t.row_id: {s for s, lst in t.retail.sites.items() if lst is not None}
        if t.oe_status_expected == "resolved"
        else set()
        for t in truth
    }
    names = sorted({s for t in truth for s in t.retail.sites} | set().union(*quoted.values()))
    sites = {}
    for site in names:
        expected = {rid for rid, s in listed.items() if site in s}
        got = {rid for rid, s in quoted.items() if site in s}
        sites[site] = SiteEval(
            expected=len(expected),
            quoted=len(got),
            found=len(expected & got),
            coverage=share(len(expected & got), len(expected)),
        )
    predicted: set[tuple[int, str]] = set()
    actual: set[tuple[int, str]] = set()
    for o in outcomes:
        t = by_id[o.row_id]
        if t.oe_status_expected != "resolved":
            continue
        actual |= {(o.row_id, s) for s in t.retail.outlier_sites}
        if isinstance(o, RetailResult):
            predicted |= {(o.row_id, s) for s in o.outliers}
    hits = len(predicted & actual)
    checks = compare_retail(outcomes, truth)
    return RetailEval(
        sites=sites,
        selected_accuracy=sum(c.selected_ok and not c.failed for c in checks) / len(checks),
        outlier_precision=share(hits, len(predicted)),
        outlier_recall=share(hits, len(actual)),
    )


def should_cost_eval(
    rows: Sequence[ResolvedRow], truth: Sequence[GroundTruthRow]
) -> ShouldCostEval:
    """Spec accuracy and landed-cost MAPE, split by own vs category-default cycle time."""
    checks = compare_should_cost([r.should_cost for r in rows], truth)
    part = [c.landed_error for c in checks if c.landed_error is not None and c.part_cycle_time]
    default = [
        c.landed_error for c in checks if c.landed_error is not None and not c.part_cycle_time
    ]
    return ShouldCostEval(
        spec_accuracy=sum(c.spec_ok and not c.failed for c in checks) / len(checks),
        landed_mape_part=_mean(part),
        landed_mape_default=_mean(default),
        n_part=len(part),
        n_default=len(default),
    )


# --- confidence and usage ---------------------------------------------------------------


def _scores(rows: Sequence[ResolvedRow]) -> list[int]:
    """Row scores in row order; an unscored row counts as 0."""
    return [r.worksheet.confidence_score or 0 for r in rows]


def calibration_eval(rows: Sequence[ResolvedRow], labels: Sequence[RowLabel]) -> CalibrationEval:
    """Calibration against "usable as is", band outcomes and the coverage-accuracy curve.

    ``labels`` must be in the same row order as ``rows``.
    """
    scores = _scores(rows)
    usable = [label.usable for label in labels]
    bands = {}
    for band in get_args(Band):
        members = [lab for r, lab in zip(rows, labels, strict=True) if r.worksheet.band == band]
        bands[band] = BandEval(
            rows=len(members),
            usable=sum(m.usable for m in members),
            row_correct=sum(m.row_correct for m in members),
        )
    curve = coverage_curve(scores, usable)
    red = bands["red"]
    return CalibrationEval(
        row_correct_rate=sum(lab.row_correct for lab in labels) / len(labels),
        usable_rate=sum(usable) / len(usable),
        ece=ece(scores, usable),
        auroc=auroc(scores, usable),
        bands=bands,
        red_row_correct_rate=share(red.row_correct, red.rows),
        reliability=reliability(scores, usable),
        curve=curve,
        thresholds=threshold_picks(curve),
    )


def _cost(agents: Sequence[AgentUsage]) -> float | None:
    """Sum of the agents' costs; None when any of them has unpriced calls."""
    costs = [a.cost_usd for a in agents]
    if any(c is None for c in costs):
        return None
    return sum(c for c in costs if c is not None)


def usage_eval(
    agents: Sequence[AgentUsage],
    models: dict[str, str],
    *,
    rows: int,
    elapsed_s: float | None,
    source: Sequence[AgentUsage] | None = None,
) -> UsageEval:
    """LLM cost of the run; a re-scored run adds the stage-agent calls of its source run."""
    own = _cost(agents)
    from_source = _cost(source) if source is not None else None
    total = (
        own
        if source is None
        else (None if own is None or from_source is None else own + from_source)
    )
    return UsageEval(
        agents=list(agents),
        models=models,
        cost_usd=own,
        source_cost_usd=from_source,
        total_cost_usd=total,
        elapsed_s=elapsed_s,
        cost_per_1k_rows=total / rows * 1000 if total is not None and rows else None,
    )


# --- error injection (spec 14 experiment) ----------------------------------------------


def injection_eval(
    rows: Sequence[ResolvedRow],
    labels: Sequence[RowLabel],
    injections: dict[int, InjectionKind],
) -> InjectionEval:
    """Bands of the injected rows and how well scores rank them below usable rows.

    ``labels`` must be in the same row order as ``rows``.
    """
    counts: dict[InjectionKind, dict[Band, int]] = {
        kind: {band: 0 for band in get_args(Band)} for kind in get_args(InjectionKind)
    }
    injected_scores, usable_scores = [], []
    for row, label in zip(rows, labels, strict=True):
        score = row.worksheet.confidence_score or 0
        kind = injections.get(row.line.row_id)
        if kind is not None:
            injected_scores.append(score)
            if row.worksheet.band is not None:
                counts[kind][row.worksheet.band] += 1
        elif label.usable:
            usable_scores.append(score)
    green = sum(c["green"] for c in counts.values())
    held_back = sum(c["yellow"] + c["red"] for c in counts.values())
    scores = [*usable_scores, *injected_scores]
    flags = [True] * len(usable_scores) + [False] * len(injected_scores)
    return InjectionEval(
        rows=len(injected_scores),
        bands_by_kind=counts,
        held_back_rate=share(held_back, len(injected_scores)),
        green_injected=green,
        auroc=auroc(scores, flags),
    )


# --- the whole evaluation ---------------------------------------------------------------


class TruthMismatch(ValueError):
    """The ground truth does not belong to the run's RFQ."""


def truth_for(rows: Sequence[ResolvedRow], truth: Sequence[GroundTruthRow]) -> list[GroundTruthRow]:
    """The truth rows of the run's rows; raises ``TruthMismatch`` if it is another RFQ's.

    Every run row needs a truth row with the same normalized customer part number. Truth
    rows beyond the run's are left out (a run made with ``--limit``).
    """
    by_id = {t.row_id: t for t in truth}
    missing = sorted(r.line.row_id for r in rows if r.line.row_id not in by_id)
    if missing:
        raise TruthMismatch(f"ground truth has no rows {missing[:5]} of the run")
    differ = sorted(
        r.line.row_id
        for r in rows
        if normalize_part_no(r.line.customer_part_no)
        != normalize_part_no(by_id[r.line.row_id].customer_part_no)
    )
    if differ:
        raise TruthMismatch(
            f"customer part numbers differ from the ground truth on rows {differ[:5]}"
        )
    return [by_id[r.line.row_id] for r in rows]


def evaluate(
    rows: Sequence[ResolvedRow],
    truth: Sequence[GroundTruthRow],
    *,
    run_id: str,
    strategy: StrategyName,
    truth_path: str,
    reviewed_rows: int,
    usage: UsageEval,
    created_at: str,
    injections: dict[int, InjectionKind] | None = None,
) -> RunEval:
    """Compare a run's (pre-review) rows with ground truth; raises ``TruthMismatch``."""
    ordered = sorted(rows, key=lambda r: r.line.row_id)
    truth = truth_for(ordered, truth)
    labels = label_rows(ordered, truth)
    return RunEval(
        run_id=run_id,
        created_at=created_at,
        strategy=strategy,
        truth_path=truth_path,
        rows=len(ordered),
        reviewed_rows=reviewed_rows,
        oe=oe_eval(ordered, truth),
        msrp=msrp_eval(ordered, truth),
        retail=retail_eval(ordered, truth),
        should_cost=should_cost_eval(ordered, truth),
        calibration=calibration_eval(ordered, labels),
        usage=usage,
        row_evals=[
            RowEval(
                row_id=r.line.row_id,
                score=r.worksheet.confidence_score,
                band=r.worksheet.band,
                row_correct=lab.row_correct,
                usable=lab.usable,
            )
            for r, lab in zip(ordered, labels, strict=True)
        ],
        injection=injection_eval(ordered, labels, injections) if injections else None,
    )
