"""Evaluation reports (spec 14): markdown plus PNG charts, for one run or a comparison.

Charts use ``matplotlib.figure.Figure`` directly rather than pyplot, so no GUI backend is
needed and no figure stays referenced after it is saved (matplotlib's guidance for servers).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import get_args

from matplotlib.figure import Figure

from rfq_agent.eval.metrics import share
from rfq_agent.schemas import Band, InjectionEval, RunEval, ThresholdPick

TARGET_FOR_COMPARE = 0.95
BANDS: tuple[Band, ...] = get_args(Band)


def pct(value: float | None) -> str:
    """A share as a percentage with one decimal; ``-`` when there is none."""
    return "-" if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} s"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return lines


# --- one run ------------------------------------------------------------------------------


def render_eval_markdown(ev: RunEval, charts: tuple[str, str] | None = None) -> str:
    """The run report; ``charts`` are the reliability and coverage PNG names to embed."""
    reliability_png, coverage_png = charts or (None, None)
    sections = [
        _intro(ev),
        _oe_section(ev),
        _msrp_section(ev),
        _retail_section(ev),
        _should_cost_section(ev),
        _calibration_section(ev, reliability_png),
        _coverage_section(ev, coverage_png),
        _injection_section(ev),
        _usage_section(ev),
    ]
    return "\n".join(line for section in sections for line in section)


def _image(alt: str, png: str | None) -> list[str]:
    return [f"![{alt}]({png})", ""] if png else []


def _intro(ev: RunEval) -> list[str]:
    plural = "" if ev.reviewed_rows == 1 else "s"
    return [
        f"# Evaluation: {ev.run_id}",
        "",
        f"Strategy `{ev.strategy}` · {ev.rows} rows · ground truth `{ev.truth_path}` · "
        f"evaluated {ev.created_at}",
        "",
        f"Rows judged on their pre-review system result: {ev.reviewed_rows} row{plural} "
        "(manual prices are not evaluated).",
        "",
    ]


def _oe_section(ev: RunEval) -> list[str]:
    oe = ev.oe
    values = [oe.coverage, oe.accuracy, oe.conclusion_accuracy, oe.chain_accuracy]
    header = ["coverage (resolved)", "accuracy (resolved rows)", "conclusion accuracy", "chain"]
    return ["## OE resolution", "", *_table(header, [[pct(v) for v in values]]), ""]


def _msrp_section(ev: RunEval) -> list[str]:
    m = ev.msrp
    values = [m.coverage, m.accuracy, m.status_accuracy, m.mean_rel_error]
    header = ["coverage", "accuracy (±1%, priced rows)", "status accuracy", "mean rel. error"]
    return ["## MSRP", "", *_table(header, [[pct(v) for v in values]]), ""]


def _retail_section(ev: RunEval) -> list[str]:
    retail = ev.retail
    sites = [
        [name, str(s.expected), str(s.quoted), str(s.found), pct(s.coverage)]
        for name, s in retail.sites.items()
    ]
    return [
        "## Retail",
        "",
        *_table(["site", "listed in truth", "quoted", "found", "coverage"], sites),
        "",
        f"Reference site accuracy {pct(retail.selected_accuracy)} · outlier precision "
        f"{pct(retail.outlier_precision)} · outlier recall {pct(retail.outlier_recall)}",
        "",
    ]


def _should_cost_section(ev: RunEval) -> list[str]:
    cost = ev.should_cost
    return [
        "## Should-cost",
        "",
        f"Spec accuracy {pct(cost.spec_accuracy)} · landed-cost MAPE "
        f"{pct(cost.landed_mape_part)} (part cycle time, n={cost.n_part})"
        f" / {pct(cost.landed_mape_default)} (category default, n={cost.n_default})",
        "",
    ]


def _calibration_section(ev: RunEval, png: str | None) -> list[str]:
    cal = ev.calibration
    bands = [
        [band, str(b.rows), str(b.usable), str(b.row_correct), pct(share(b.usable, b.rows))]
        for band, b in cal.bands.items()
    ]
    bins = [
        [f"{b.lo}-{b.hi}", str(b.rows), _num(b.mean_score), pct(b.usable_rate)]
        for b in cal.reliability
    ]
    return [
        "## Confidence calibration",
        "",
        f"Label: usable as is (spec 12.2). Row correct {pct(cal.row_correct_rate)} · usable "
        f"{pct(cal.usable_rate)} · ECE {_num(cal.ece)} · AUROC {_num(cal.auroc)} · red rows "
        f"that are correct (rightly held back) {pct(cal.red_row_correct_rate)}",
        "",
        *_table(["band", "rows", "usable", "row correct", "usable rate"], bands),
        "",
        *_table(["score bin", "rows", "mean score", "usable rate"], bins),
        "",
        *_image("reliability diagram", png),
    ]


def _coverage_section(ev: RunEval, png: str | None) -> list[str]:
    picks = [
        [pct(p.target), "-" if p.threshold is None else str(p.threshold), pct(p.coverage)]
        for p in ev.calibration.thresholds
    ]
    return [
        "## Coverage-accuracy",
        "",
        "Rows scoring at or above the threshold are handled automatically; accuracy is the "
        "share usable as is among them.",
        "",
        *_table(["target accuracy", "lowest threshold", "coverage"], picks),
        "",
        *_image("coverage-accuracy curve", png),
    ]


def _injection_section(ev: RunEval) -> list[str]:
    inj = ev.injection
    if inj is None:
        return []
    kinds = [[kind, *(str(bands[b]) for b in BANDS)] for kind, bands in inj.bands_by_kind.items()]
    return [
        "## Injected errors",
        "",
        f"Rows made wrong on purpose (spec 14): {injection_summary(inj)}",
        "",
        *_table(["kind", *BANDS], kinds),
        "",
    ]


def injection_summary(inj: InjectionEval) -> str:
    """One line on the injected rows: how many the scores held back (CLI and report)."""
    return (
        f"injected {inj.rows} rows: held back (yellow or red) {pct(inj.held_back_rate)}, "
        f"left in green {inj.green_injected}, AUROC usable vs injected {_num(inj.auroc)}"
    )


def _usage_section(ev: RunEval) -> list[str]:
    usage = ev.usage
    agents = [
        [
            a.agent,
            str(a.calls),
            str(a.errors),
            str(a.input_tokens),
            str(a.output_tokens),
            _usd(a.cost_usd),
        ]
        for a in usage.agents
    ]
    source = (
        f" · source run (stage agents) {_usd(usage.source_cost_usd)}"
        if usage.source_cost_usd is not None
        else ""
    )
    return [
        "## Cost and time",
        "",
        *_table(["agent", "calls", "errors", "input tokens", "output tokens", "cost"], agents),
        "",
        f"Models: {_models(ev)} · this run {_usd(usage.cost_usd)}{source}"
        f" · total {_usd(usage.total_cost_usd)} · per 1k rows {_usd(usage.cost_per_1k_rows)}"
        f" · elapsed {_seconds(usage.elapsed_s)} (wall clock)",
        "",
    ]


def _models(ev: RunEval) -> str:
    return ", ".join(f"{role} {model}" for role, model in sorted(ev.usage.models.items())) or "-"


def write_eval_report(ev: RunEval, out_dir: Path) -> list[Path]:
    """``eval_<run>.md`` with its reliability and coverage-accuracy PNG charts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"eval_{ev.run_id}"
    reliability_png = out_dir / f"{stem}_reliability.png"
    coverage_png = out_dir / f"{stem}_coverage.png"
    _reliability_chart([ev], reliability_png)
    _coverage_chart([ev], coverage_png)
    md = out_dir / f"{stem}.md"
    md.write_text(render_eval_markdown(ev, (reliability_png.name, coverage_png.name)) + "\n")
    return [md, reliability_png, coverage_png]


# --- comparison ---------------------------------------------------------------------------


def _label(ev: RunEval) -> str:
    return f"{ev.run_id} ({ev.strategy})"


def _pick(ev: RunEval) -> ThresholdPick | None:
    """The run's lowest threshold reaching the comparison's target accuracy."""
    return next((p for p in ev.calibration.thresholds if p.target == TARGET_FOR_COMPARE), None)


def render_compare_markdown(evals: Sequence[RunEval]) -> str:
    """One line per run: the numbers for choosing a strategy and thresholds (spec 14)."""
    if len(evals) < 2:
        raise ValueError("a comparison needs at least two runs")
    header = [
        "run",
        "strategy",
        "models",
        "rows",
        "green / yellow / red",
        "row correct",
        "usable",
        "ECE",
        "AUROC",
        "green usable",
        "red row correct",
        f"threshold @{TARGET_FOR_COMPARE:.0%} (coverage)",
        "injected held back",
        "injected in green",
        "injection AUROC",
        "LLM calls (this run)",
        "cost (this run)",
        "total cost",
        "per 1k rows",
        "elapsed",
    ]
    rows = []
    for ev in evals:
        cal, usage = ev.calibration, ev.usage
        green = cal.bands["green"]
        pick = _pick(ev)
        threshold = (
            "-"
            if pick is None or pick.threshold is None
            else f"{pick.threshold} ({pct(pick.coverage)})"
        )
        rows.append(
            [
                ev.run_id,
                ev.strategy,
                _models(ev),
                str(ev.rows),
                " / ".join(str(cal.bands[b].rows) for b in BANDS),
                pct(cal.row_correct_rate),
                pct(cal.usable_rate),
                _num(cal.ece),
                _num(cal.auroc),
                pct(share(green.usable, green.rows)),
                pct(cal.red_row_correct_rate),
                threshold,
                *_injection_cells(ev),
                str(sum(a.calls for a in usage.agents)),
                _usd(usage.cost_usd),
                _usd(usage.total_cost_usd),
                _usd(usage.cost_per_1k_rows),
                _seconds(usage.elapsed_s),
            ]
        )
    return "\n".join(
        [
            "# Strategy comparison",
            "",
            *_table(header, rows),
            "",
            "Usable = usable as is (spec 12.2). Total cost of a re-scored run includes the stage "
            "agents of its source run; its elapsed time covers only its own finalize.",
        ]
    )


def _injection_cells(ev: RunEval) -> list[str]:
    """Held-back share, injected rows left in green, AUROC; ``-`` without injected errors."""
    inj = ev.injection
    if inj is None:
        return ["-", "-", "-"]
    return [pct(inj.held_back_rate), f"{inj.green_injected} / {inj.rows}", _num(inj.auroc)]


def write_compare_report(evals: Sequence[RunEval], out_dir: Path, *, stamp: str) -> list[Path]:
    """``compare_<stamp>.md`` with overlaid reliability and coverage-accuracy charts."""
    text = render_compare_markdown(evals)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"compare_{stamp}"
    reliability_png = out_dir / f"{stem}_reliability.png"
    coverage_png = out_dir / f"{stem}_coverage.png"
    _reliability_chart(evals, reliability_png)
    _coverage_chart(evals, coverage_png)
    md = out_dir / f"{stem}.md"
    md.write_text(
        f"{text}\n\n![reliability diagram]({reliability_png.name})\n\n"
        f"![coverage-accuracy curve]({coverage_png.name})\n"
    )
    return [md, reliability_png, coverage_png]


# --- charts -------------------------------------------------------------------------------


def _reliability_chart(evals: Sequence[RunEval], path: Path) -> None:
    """Mean score vs usable rate per bin, one line per run, with the diagonal for reference."""
    fig = Figure(figsize=(6, 5))
    ax = fig.subplots()
    ax.plot([0, 1], [0, 1], linestyle="--", color="grey", label="perfect calibration")
    for ev in evals:
        points = [
            (b.mean_score, b.usable_rate)
            for b in ev.calibration.reliability
            if b.mean_score is not None and b.usable_rate is not None
        ]
        ax.plot([p[0] for p in points], [p[1] for p in points], marker="o", label=_label(ev))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("mean score / 100")
    ax.set_ylabel("share usable as is")
    ax.set_title("Reliability diagram")
    ax.legend(loc="lower right", fontsize="small")
    fig.savefig(path, format="png", dpi=120)


def _coverage_chart(evals: Sequence[RunEval], path: Path) -> None:
    """Coverage and accuracy against the auto-handling threshold, one pair per run."""
    fig = Figure(figsize=(7, 5))
    ax = fig.subplots()
    for index, ev in enumerate(evals):
        curve = ev.calibration.curve
        thresholds = [p.threshold for p in curve]
        (line,) = ax.plot(thresholds, [p.coverage for p in curve], label=f"coverage {_label(ev)}")
        ax.plot(
            thresholds,
            [p.accuracy if p.accuracy is not None else float("nan") for p in curve],
            linestyle="--",
            color=line.get_color(),
            label=f"accuracy {_label(ev)}",
        )
        pick = _pick(ev)
        if pick is not None and pick.threshold is not None:
            ax.axvline(pick.threshold, color=line.get_color(), linewidth=0.8, linestyle="-.")
            ax.annotate(
                f"t={pick.threshold} ({TARGET_FOR_COMPARE:.0%})",
                (pick.threshold, 0.62 - 0.06 * index),  # clear of the legend
                xytext=(3, 0),
                textcoords="offset points",
                fontsize="small",
                color=line.get_color(),
            )
    ax.axhline(TARGET_FOR_COMPARE, color="grey", linewidth=0.8, linestyle=":")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("threshold (score >= t handled automatically)")
    ax.set_ylabel("share")
    ax.set_title("Coverage-accuracy")
    ax.legend(loc="lower left", fontsize="small")
    fig.savefig(path, format="png", dpi=120)
