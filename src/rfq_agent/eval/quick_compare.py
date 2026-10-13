"""Row-by-row comparison of agent outcomes with ground truth (manual review aid).

Correctness follows the P1 rules: ``resolved`` must equal the primary OE (and the
chain must match); ``multiple`` must cover every accepted OE; ``not_found`` must match.
Price agents skip rows whose OE is not uniquely resolved (P3-Q2, P4-Q1).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar, get_args

from rfq_agent.agents.common import RowFailure
from rfq_agent.data_gen.truth import GroundTruthRow
from rfq_agent.partno import normalize_part_no
from rfq_agent.pricing import msrp_matches
from rfq_agent.schemas import (
    Band,
    MSRPResult,
    OEResolution,
    ResolvedRow,
    RetailResult,
    ShouldCostResult,
    SpecSource,
    weakest_spec_source,
)


@dataclass
class RowCheck:
    row_id: int
    customer_part_no: str
    tags: list[str]
    expected_status: str
    failed: bool
    expected: str  # display only
    got: str  # display only
    oe_ok: bool
    chain_ok: bool | None
    our_part_ok: bool
    note: str


def _norm(values: Sequence[str]) -> list[str]:
    return [normalize_part_no(v) for v in values]


def _mark(value: bool | None) -> str:
    return "-" if value is None else ("✓" if value else "✗")


def _count_by(statuses: Sequence[str], ok: Sequence[bool]) -> list[str]:
    """``"<status> <correct>/<total>"`` for each expected status, sorted by status."""
    total = Counter(statuses)
    correct = Counter(s for s, good in zip(statuses, ok, strict=True) if good)
    return [f"{s} {correct[s]}/{total[s]}" for s in sorted(total)]


def compare(
    outcomes: Sequence[OEResolution | RowFailure], truth: Sequence[GroundTruthRow]
) -> list[RowCheck]:
    by_id = {t.row_id: t for t in truth}
    checks = []
    for outcome in sorted(outcomes, key=lambda o: o.row_id):
        t = by_id[outcome.row_id]
        expected_desc = t.oe_status_expected + (f" {t.primary_oe}" if t.primary_oe else "")
        expected_our = t.our_part_no if t.our_part_reachability != "unreachable" else None
        if isinstance(outcome, RowFailure):
            checks.append(
                RowCheck(
                    row_id=outcome.row_id,
                    customer_part_no=t.customer_part_no,
                    tags=t.tags,
                    expected_status=t.oe_status_expected,
                    failed=True,
                    expected=expected_desc,
                    got=f"FAILED@{outcome.stage}",
                    oe_ok=False,
                    chain_ok=None,
                    our_part_ok=False,
                    note=outcome.error[:80],
                )
            )
            continue
        chain_ok: bool | None = None
        if outcome.status != t.oe_status_expected:
            oe_ok = False
        elif outcome.status == "resolved":
            oe_ok = normalize_part_no(outcome.current_oe or "") == normalize_part_no(
                t.primary_oe or ""
            )
            chain_ok = _norm(outcome.supersession_chain) == _norm(t.supersession_chain)
        elif outcome.status == "multiple":
            got = {normalize_part_no(c.oe_part_no) for c in outcome.candidates}
            oe_ok = set(_norm(t.accepted_oes)) <= got
        else:
            oe_ok = True
        got_desc = outcome.status + (f" {outcome.current_oe}" if outcome.current_oe else "")
        if outcome.status == "multiple":
            got_desc += " [" + ", ".join(c.oe_part_no for c in outcome.candidates) + "]"
        checks.append(
            RowCheck(
                row_id=outcome.row_id,
                customer_part_no=t.customer_part_no,
                tags=t.tags,
                expected_status=t.oe_status_expected,
                failed=False,
                expected=expected_desc,
                got=got_desc,
                oe_ok=oe_ok,
                chain_ok=chain_ok,
                our_part_ok=outcome.our_part_no == expected_our,
                note="",
            )
        )
    return checks


def render_markdown(checks: Sequence[RowCheck]) -> str:
    lines = [
        "| row | customer part | tags | expected | got | OE | chain | our part | note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        lines.append(
            f"| {c.row_id} | {c.customer_part_no} | {','.join(c.tags)} | {c.expected} | "
            f"{c.got} | {_mark(c.oe_ok)} | {_mark(c.chain_ok)} | {_mark(c.our_part_ok)} | "
            f"{c.note} |"
        )
    lines.append("")
    lines.append(summary(checks))
    return "\n".join(lines)


def summary(checks: Sequence[RowCheck]) -> str:
    n = len(checks)
    chains = [c.chain_ok for c in checks if c.chain_ok is not None]
    parts = [
        f"OE correct {sum(c.oe_ok for c in checks)}/{n}",
        *_count_by([c.expected_status for c in checks], [c.oe_ok for c in checks]),
        f"chain correct {sum(chains)}/{len(chains)}",
        f"our part correct {sum(c.our_part_ok for c in checks)}/{n}",
        f"failed rows {sum(c.failed for c in checks)}",
    ]
    return "**Summary:** " + " · ".join(parts)


# --- oe_msrp ------------------------------------------------------------------------


@dataclass
class MSRPCheck:
    row_id: int
    customer_part_no: str
    tags: list[str]
    expected_status: str
    failed: bool
    expected: str  # display only
    got: str  # display only
    status_ok: bool
    price_ok: bool | None
    source_ok: bool | None
    skipped_multiple: bool
    note: str


def _expected_msrp_status(t: GroundTruthRow) -> str:
    # Plan decision P3-Q2: rows whose OE is "multiple" are not looked up.
    return "not_found" if t.oe_status_expected == "multiple" else t.msrp.status


def compare_msrp(
    outcomes: Sequence[MSRPResult | RowFailure], truth: Sequence[GroundTruthRow]
) -> list[MSRPCheck]:
    by_id = {t.row_id: t for t in truth}
    checks = []
    for outcome in sorted(outcomes, key=lambda o: o.row_id):
        t = by_id[outcome.row_id]
        multiple = t.oe_status_expected == "multiple"
        want_status = _expected_msrp_status(t)
        expected = want_status
        if want_status != "not_found" and t.msrp.value is not None:
            expected += f" {t.msrp.value:.2f} via {t.msrp.source_oe}"
        if multiple:
            expected += " (OE multiple: skipped by design)"
        if isinstance(outcome, RowFailure):
            checks.append(
                MSRPCheck(
                    row_id=outcome.row_id,
                    customer_part_no=t.customer_part_no,
                    tags=t.tags,
                    expected_status=want_status,
                    failed=True,
                    expected=expected,
                    got=f"FAILED@{outcome.stage}",
                    status_ok=False,
                    price_ok=None,
                    source_ok=None,
                    skipped_multiple=multiple,
                    note=outcome.error[:80],
                )
            )
            continue
        quote = outcome.quote
        got: str = outcome.status
        price_ok: bool | None = None
        source_ok: bool | None = None
        if quote is not None:
            got += f" {quote.effective_price:.2f} via {quote.matched_part_no}"
            if t.msrp.value is not None and quote.effective_price is not None:
                price_ok = msrp_matches(quote.effective_price, t.msrp.value)
            else:
                price_ok = False
            source_ok = normalize_part_no(quote.matched_part_no) == normalize_part_no(
                t.msrp.source_oe or ""
            )
        checks.append(
            MSRPCheck(
                row_id=outcome.row_id,
                customer_part_no=t.customer_part_no,
                tags=t.tags,
                expected_status=want_status,
                failed=False,
                expected=expected,
                got=got,
                status_ok=outcome.status == want_status,
                price_ok=price_ok,
                source_ok=source_ok,
                skipped_multiple=multiple,
                note="",
            )
        )
    return checks


def msrp_correct(check: MSRPCheck) -> bool:
    return check.status_ok and check.price_ok is not False and check.source_ok is not False


def render_msrp_markdown(checks: Sequence[MSRPCheck]) -> str:
    lines = [
        "| row | customer part | tags | expected | got | status | price ±1% | source | note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        lines.append(
            f"| {c.row_id} | {c.customer_part_no} | {','.join(c.tags)} | {c.expected} | "
            f"{c.got} | {_mark(c.status_ok)} | {_mark(c.price_ok)} | {_mark(c.source_ok)} | "
            f"{c.note} |"
        )
    lines.append("")
    lines.append(msrp_summary(checks))
    return "\n".join(lines)


def msrp_summary(checks: Sequence[MSRPCheck]) -> str:
    n = len(checks)
    ok = [msrp_correct(c) for c in checks]
    fallback = [c for c in checks if "msrp_fallback" in c.tags]
    parts = [
        f"MSRP correct {sum(ok)}/{n}",
        *_count_by([c.expected_status for c in checks], ok),
        f"fallback rows {sum(msrp_correct(c) for c in fallback)}/{len(fallback)}",
        f"OE-multiple rows skipped {sum(c.skipped_multiple for c in checks)}",
        f"failed rows {sum(c.failed for c in checks)}",
    ]
    return "**Summary:** " + " · ".join(parts)


# --- retail_price ---------------------------------------------------------------------

RETAIL_TOLERANCE = 0.01  # same relative tolerance as MSRP


@dataclass
class RetailCheck:
    row_id: int
    customer_part_no: str
    tags: list[str]
    skipped: bool  # OE not uniquely resolved: no quotes expected
    failed: bool
    expected: str  # display only
    got: str  # display only
    sites_ok: bool
    price_ok: bool | None
    stock_ok: bool | None
    match_ok: bool | None
    outliers_ok: bool
    selected_ok: bool
    prices_correct: int
    prices_total: int
    note: str

    @property
    def correct(self) -> bool:
        checks = (self.sites_ok, self.price_ok, self.stock_ok, self.match_ok)
        return all(c is not False for c in checks) and self.outliers_ok and self.selected_ok


def compare_retail(
    outcomes: Sequence[RetailResult | RowFailure], truth: Iterable[GroundTruthRow]
) -> list[RetailCheck]:
    by_id = {t.row_id: t for t in truth}
    return [_retail_check(o, by_id[o.row_id]) for o in sorted(outcomes, key=lambda o: o.row_id)]


def _retail_check(outcome: RetailResult | RowFailure, t: GroundTruthRow) -> RetailCheck:
    skipped = t.oe_status_expected != "resolved"
    want = {} if skipped else {s: lst for s, lst in t.retail.sites.items() if lst is not None}
    want_outliers = [] if skipped else sorted(t.retail.outlier_sites)
    want_selected = None if skipped else t.retail.expected_selected_site
    check = RetailCheck(
        row_id=t.row_id,
        customer_part_no=t.customer_part_no,
        tags=t.tags,
        skipped=skipped,
        failed=isinstance(outcome, RowFailure),
        expected=_retail_desc(sorted(want), want_outliers, want_selected, skipped),
        got="",
        sites_ok=False,
        price_ok=None,
        stock_ok=None,
        match_ok=None,
        outliers_ok=False,
        selected_ok=False,
        prices_correct=0,
        prices_total=len(want),
        note="",
    )
    if isinstance(outcome, RowFailure):
        check.got, check.note = f"FAILED@{outcome.stage}", outcome.error[:80]
        return check
    got = {q.source: q for q in outcome.quotes}
    common = sorted(set(want) & set(got))
    price_hits = [
        s for s in common if _price_close(got[s].effective_price, want[s].effective_price)
    ]
    selected = outcome.selected.source if outcome.selected else None
    check.got = _retail_desc(sorted(got), sorted(outcome.outliers), selected, False)
    check.sites_ok = set(got) == set(want)
    if common:
        check.price_ok = len(price_hits) == len(common)
        check.stock_ok = all(got[s].in_stock == want[s].in_stock for s in common)
        check.match_ok = all(got[s].match_quality == want[s].match_quality for s in common)
    check.outliers_ok = sorted(outcome.outliers) == want_outliers
    check.selected_ok = selected == want_selected
    check.prices_correct = len(price_hits)
    return check


def _price_close(got: float | None, want: float) -> bool:
    return got is not None and abs(got - want) / want <= RETAIL_TOLERANCE


def _retail_desc(sites: list[str], outliers: list[str], selected: str | None, skipped: bool) -> str:
    if skipped:
        return "no quotes (OE not resolved: skipped by design)"
    return (
        f"sites {','.join(sites) or '-'}; outliers {','.join(outliers) or '-'}; "
        f"selected {selected or '-'}"
    )


def render_retail_markdown(checks: Sequence[RetailCheck]) -> str:
    lines = [
        "| row | customer part | tags | expected | got | sites | price ±1% | stock | match "
        "| outliers | selected | note |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        lines.append(
            f"| {c.row_id} | {c.customer_part_no} | {','.join(c.tags)} | {c.expected} | "
            f"{c.got} | {_mark(c.sites_ok)} | {_mark(c.price_ok)} | {_mark(c.stock_ok)} | "
            f"{_mark(c.match_ok)} | {_mark(c.outliers_ok)} | {_mark(c.selected_ok)} | "
            f"{c.note} |"
        )
    lines.append("")
    lines.append(retail_summary(checks))
    return "\n".join(lines)


def retail_summary(checks: Sequence[RetailCheck]) -> str:
    n = len(checks)
    parts = [
        f"retail correct {sum(c.correct for c in checks)}/{n}",
        f"site prices {sum(c.prices_correct for c in checks)}/"
        f"{sum(c.prices_total for c in checks)}",
        f"outliers correct {sum(c.outliers_ok for c in checks)}/{n}",
        f"selected correct {sum(c.selected_ok for c in checks)}/{n}",
        f"OE-skipped rows {sum(c.skipped for c in checks)}",
        f"failed rows {sum(c.failed for c in checks)}",
    ]
    return "**Summary:** " + " · ".join(parts)


# --- should_cost ------------------------------------------------------------------------

COST_TOLERANCE = 0.01  # part cycle time rows: relative error <= 1% counts as correct
EVAL_WEIGHT_TOLERANCE = 0.01  # comparison only: page weights in g / lb lose a little precision


@dataclass
class ExpectedCost:
    """What should_cost should output for a row, derived from ground truth alone (P5-Q15)."""

    status: str
    spec_source: str
    category: str | None
    material: str | None
    weight_kg: float | None
    part_cycle_time: bool  # False: the category default cycle time applies


def expected_should_cost(t: GroundTruthRow) -> ExpectedCost:
    """Re-derive the P5 rules (spec 11.4) from what the generator knows about a row.

    The internal database and every uncorrupted page hold the true spec; a
    ``spec_conflict`` page contradicts the (complete) internal record, which wins.
    """
    known = t.our_part_reachability != "unreachable"
    page = t.oe_status_expected == "resolved" and t.external_spec_available
    material_source = _expected_field(known and t.internal_spec.material is not None, page)
    weight_source = _expected_field(known and t.internal_spec.weight_kg is not None, page)
    if "spec_conflict" in t.tags and page:
        material_source = "conflict"  # which field the page corrupts is not recorded
    spec_source = weakest_spec_source((material_source, weight_source))
    # Unknown parts take their category from the OEM catalog page of a resolved OE.
    category = t.spec.category if known or t.oe_status_expected == "resolved" else None
    estimated = spec_source != "missing" and category is not None
    return ExpectedCost(
        status="estimated" if estimated else "insufficient_data",
        spec_source=spec_source,
        category=category,
        material=t.spec.material if material_source != "missing" else None,
        weight_kg=t.spec.weight_kg if weight_source != "missing" else None,
        part_cycle_time=known,
    )


def _expected_field(internal: bool, page: bool) -> SpecSource:
    if internal:
        return "both_agree" if page else "internal"
    return "external" if page else "missing"


@dataclass
class ShouldCostCheck:
    row_id: int
    customer_part_no: str
    tags: list[str]
    part_cycle_time: bool  # False: category default cycle time, cost error reported only
    failed: bool
    expected: str  # display only
    got: str  # display only
    status_ok: bool
    spec_ok: bool  # category, material key and weight (<= 1%)
    spec_source_ok: bool
    cost_ok: bool | None  # None: not judged (category default cycle time or not estimable)
    landed_error: float | None  # relative, when both sides have a cost stack
    fob_error: float | None
    insufficient: bool  # the agent reported insufficient_data
    note: str

    @property
    def correct(self) -> bool:
        return (
            not self.failed
            and self.status_ok
            and self.spec_ok
            and self.spec_source_ok
            and self.cost_ok is not False
        )


def compare_should_cost(
    outcomes: Sequence[ShouldCostResult | RowFailure], truth: Iterable[GroundTruthRow]
) -> list[ShouldCostCheck]:
    by_id = {t.row_id: t for t in truth}
    return [
        _should_cost_check(o, by_id[o.row_id]) for o in sorted(outcomes, key=lambda o: o.row_id)
    ]


def _should_cost_check(
    outcome: ShouldCostResult | RowFailure, t: GroundTruthRow
) -> ShouldCostCheck:
    want = expected_should_cost(t)
    judged = want.status == "estimated" and want.part_cycle_time
    check = ShouldCostCheck(
        row_id=t.row_id,
        customer_part_no=t.customer_part_no,
        tags=t.tags,
        part_cycle_time=want.part_cycle_time,
        failed=isinstance(outcome, RowFailure),
        expected=_cost_desc(want.status, want.spec_source, want.material, want.weight_kg),
        got="",
        status_ok=False,
        spec_ok=False,
        spec_source_ok=False,
        cost_ok=False if judged else None,
        landed_error=None,
        fob_error=None,
        insufficient=False,
        note="",
    )
    if isinstance(outcome, RowFailure):
        check.got, check.note = f"FAILED@{outcome.stage}", outcome.error[:80]
        return check
    check.insufficient = outcome.status == "insufficient_data"
    check.got = _cost_desc(outcome.status, outcome.spec_source, outcome.material, outcome.weight_kg)
    check.status_ok = outcome.status == want.status
    # With no expected category the agent may still take one from the RFQ description,
    # which ground truth does not record; only an expected category is judged.
    category_ok = want.category is None or outcome.category == want.category
    check.spec_ok = (
        category_ok
        and outcome.material == want.material
        and _weight_close(outcome.weight_kg, want.weight_kg)
    )
    check.spec_source_ok = outcome.spec_source == want.spec_source
    if want.status == "estimated" and outcome.breakdown is not None:
        check.landed_error = _relative_error(outcome.breakdown.landed_cost, t.cost.landed_cost)
        check.fob_error = _relative_error(outcome.breakdown.fob, t.cost.fob)
        if judged:
            check.cost_ok = check.landed_error <= COST_TOLERANCE and (
                check.fob_error <= COST_TOLERANCE
            )
    return check


def _weight_close(got: float | None, want: float | None) -> bool:
    if got is None or want is None:
        return got is want
    return _relative_error(got, want) <= EVAL_WEIGHT_TOLERANCE


def _relative_error(got: float, want: float) -> float:
    return abs(got - want) / want


def _cost_desc(status: str, source: str, material: str | None, weight: float | None) -> str:
    weight_text = f"{weight:.3f} kg" if weight is not None else "-"
    return f"{status} ({source}; {material or '-'}, {weight_text})"


def render_should_cost_markdown(checks: Sequence[ShouldCostCheck]) -> str:
    lines = [
        "| row | customer part | tags | expected | got | status | spec | spec_source "
        "| landed err | cost | note |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        error = "-" if c.landed_error is None else f"{c.landed_error:.1%}"
        if c.landed_error is not None and not c.part_cycle_time:
            error += " (default cycle)"
        lines.append(
            f"| {c.row_id} | {c.customer_part_no} | {','.join(c.tags)} | {c.expected} | "
            f"{c.got} | {_mark(c.status_ok)} | {_mark(c.spec_ok)} | "
            f"{_mark(c.spec_source_ok)} | {error} | {_mark(c.cost_ok)} | {c.note} |"
        )
    lines.append("")
    lines.append(should_cost_summary(checks))
    return "\n".join(lines)


def should_cost_summary(checks: Sequence[ShouldCostCheck]) -> str:
    n = len(checks)
    judged = [c for c in checks if c.cost_ok is not None]
    parts = [
        f"should-cost correct {sum(c.correct for c in checks)}/{n}",
        f"spec correct {sum(c.spec_ok for c in checks)}/{n}",
        f"spec_source correct {sum(c.spec_source_ok for c in checks)}/{n}",
        f"cost correct {sum(c.cost_ok is True for c in judged)}/{len(judged)} (part cycle time)",
        f"landed MAPE {_mape(checks, True)} (part cycle time, n={len(_errors(checks, True))}) / "
        f"{_mape(checks, False)} (category default, n={len(_errors(checks, False))})",
        f"insufficient_data {sum(c.insufficient for c in checks)}",
        f"failed rows {sum(c.failed for c in checks)}",
    ]
    return "**Summary:** " + " · ".join(parts)


def _errors(checks: Sequence[ShouldCostCheck], part_cycle_time: bool) -> list[float]:
    return [
        c.landed_error
        for c in checks
        if c.landed_error is not None and c.part_cycle_time == part_cycle_time
    ]


def _mape(checks: Sequence[ShouldCostCheck], part_cycle_time: bool) -> str:
    errors = _errors(checks, part_cycle_time)
    return f"{sum(errors) / len(errors):.1%}" if errors else "-"


# --- worksheet (resolution + confidence) ------------------------------------------------


_Shared = TypeVar("_Shared", MSRPResult | RowFailure, RetailResult | RowFailure)


@dataclass
class WorksheetCheck:
    """One worksheet row judged by the P6 "row correct" rule (plan phase-6 Q5)."""

    row_id: int
    customer_part_no: str
    score: int | None
    band: Band | None
    oe_ok: bool
    msrp_ok: bool
    retail_ok: bool
    cost_ok: bool
    reasons: list[str]

    @property
    def correct(self) -> bool:
        return self.oe_ok and self.msrp_ok and self.retail_ok and self.cost_ok


def compare_worksheet(
    rows: Sequence[ResolvedRow], truth: Sequence[GroundTruthRow]
) -> list[WorksheetCheck]:
    """Judge each row on what the worksheet shows (shared rows use the reused results)."""
    oe = {c.row_id: c for c in compare([r.oe for r in rows], truth)}
    msrp = {c.row_id: c for c in compare_msrp([own_result(r.msrp, r) for r in rows], truth)}
    retail = {c.row_id: c for c in compare_retail([own_result(r.retail, r) for r in rows], truth)}
    cost = {c.row_id: c for c in compare_should_cost([r.should_cost for r in rows], truth)}
    checks = []
    for row in sorted(rows, key=lambda r: r.line.row_id):
        rid, ws = row.line.row_id, row.worksheet
        m, c = msrp[rid], cost[rid]
        checks.append(
            WorksheetCheck(
                row_id=rid,
                customer_part_no=ws.customer_part_no,
                score=ws.confidence_score,
                band=ws.band,
                oe_ok=not oe[rid].failed and oe[rid].oe_ok,
                msrp_ok=not m.failed and m.status_ok and m.price_ok is not False,
                retail_ok=not retail[rid].failed and retail[rid].selected_ok,
                cost_ok=not c.failed and c.status_ok and c.spec_ok,
                reasons=ws.review_reasons,
            )
        )
    return checks


def own_result(outcome: _Shared, row: ResolvedRow) -> _Shared:
    """A shared row's reused result, relabelled with this row's id for the comparison."""
    return outcome.model_copy(update={"row_id": row.line.row_id})


def render_worksheet_markdown(checks: Sequence[WorksheetCheck]) -> str:
    lines = [
        "| row | score | band | row correct | OE | MSRP | retail | should-cost | review reasons |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        lines.append(
            f"| {c.row_id} | {c.score} | {c.band} | {_mark(c.correct)} | {_mark(c.oe_ok)} | "
            f"{_mark(c.msrp_ok)} | {_mark(c.retail_ok)} | {_mark(c.cost_ok)} | "
            f"{'; '.join(c.reasons) or '-'} |"
        )
    return "\n".join(lines) + "\n\n" + worksheet_summary(checks)


def worksheet_summary(checks: Sequence[WorksheetCheck]) -> str:
    parts = []
    for band in get_args(Band):
        in_band = [c for c in checks if c.band == band]
        good = sum(c.correct for c in in_band)
        parts.append(f"{band} {good}/{len(in_band)}")
    total = sum(c.correct for c in checks)
    return f"row correct {total}/{len(checks)} (by band: {', '.join(parts)})"
