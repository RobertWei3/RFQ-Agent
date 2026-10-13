"""Pure functions from API JSON to the tables and charts the pages draw."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, NamedTuple, cast

import altair as alt
import pandas as pd
from pandas.io.formats.style import Styler

from dashboard.api_client import Json

# What ``.properties()`` returns for the charts below (altair leaves it untyped).
AltairChart = alt.Chart | alt.LayerChart | alt.FacetChart

FOB_ITEMS = ("material", "conversion", "overhead", "sga", "profit", "packaging")
LANDED_ITEMS = ("freight", "insurance", "duty", "mpf", "brokerage", "inland_freight")
# The evidence signals of spec 12.2, in the order the spec lists them.
EVIDENCE_SIGNALS = (
    "oe_match",
    "chain",
    "price_sources",
    "retail_dispersion",
    "msrp_retail",
    "spec_source",
)
GATE_PREFIX = "gate_"
ROLE_COLORS = {"selected": "#2E7D32", "outlier": "#C62828", "other": "#90A4AE"}
ROLE_EXPR = "{'selected': '选中', 'outlier': '离群', 'other': '其他'}[datum.label]"
QUOTE_FIELDS = (
    "effective_price",
    "list_price",
    "sale_price",
    "discount_pct",
    "in_stock",
    "match_quality",
    "matched_part_no",
)


def waterfall(breakdown: Json) -> list[dict[str, Any]]:
    """Floating bars material -> ... -> FOB (subtotal) -> ... -> landed cost (total)."""
    steps: list[dict[str, Any]] = []

    def add(item: str, start: float, end: float, kind: str) -> None:
        steps.append({"order": len(steps), "item": item, "start": start, "end": end, "kind": kind})

    level = 0.0
    for item in FOB_ITEMS:
        add(item, level, level + breakdown[item], "cost")
        level += breakdown[item]
    add("fob", 0.0, breakdown["fob"], "subtotal")
    level = breakdown["fob"]
    for item in LANDED_ITEMS:
        add(item, level, level + breakdown[item], "cost")
        level += breakdown[item]
    add("landed_cost", 0.0, breakdown["landed_cost"], "total")
    return steps


def is_failure(stage_result: Json) -> bool:
    """A stage result is a RowFailure (``stage`` + ``error``) rather than the stage's output."""
    return isinstance(stage_result, dict) and "stage" in stage_result and "error" in stage_result


def site_prices(retail: Json) -> list[dict[str, Any]]:
    """One bar per site quote, its role: the selected quote, an outlier, or another quote."""
    if is_failure(retail):
        return []
    selected = (retail.get("selected") or {}).get("source")
    outliers = set(retail.get("outliers") or [])
    bars = []
    for q in retail.get("quotes") or []:
        role = (
            "selected"
            if q["source"] == selected
            else "outlier"
            if q["source"] in outliers
            else "other"
        )
        bars.append({"site": q["source"], **{k: q.get(k) for k in QUOTE_FIELDS}, "role": role})
    return bars


class SignalParts(NamedTuple):
    """A row's confidence signals: evidence (0-1), triggered gates (cap), and LLM parts."""

    evidence: list[tuple[str, float]]
    gates: list[tuple[str, int]]
    llm: list[tuple[str, float]]


def split_signals(signals: dict[str, float]) -> SignalParts:
    evidence = [(name, signals[name]) for name in EVIDENCE_SIGNALS if name in signals]
    gates = [
        (name.removeprefix(GATE_PREFIX), int(value))
        for name, value in signals.items()
        if name.startswith(GATE_PREFIX)
    ]
    llm = [
        (name, value)
        for name, value in signals.items()
        if name not in EVIDENCE_SIGNALS and not name.startswith(GATE_PREFIX)
    ]
    return SignalParts(evidence, gates, llm)


# Band fills of the Excel worksheet (spec 12.3).
BAND_COLORS = {"green": "#C6EFCE", "yellow": "#FFEB9C", "red": "#FFC7CE"}
REASONING_PATHS = ("oe", "msrp", "retail", "should_cost")
INT_COLUMNS = ("row_id", "price_source_row", "confidence_score")
WATERFALL_COLORS = {"cost": "#4C78A8", "subtotal": "#9ECAE9", "total": "#2F4B7C"}


def _joined(sep: str, key: str) -> Callable[[Json], str]:
    return lambda w: sep.join(str(v) for v in w.get(key) or [])


def _field(key: str) -> Callable[[Json], Any]:
    return lambda w: w.get(key)


def _columns(sites: Sequence[str]) -> list[tuple[str, Callable[[Json], Any]]]:
    """The Excel worksheet's columns, in its order (spec 12.3)."""

    def site_price(site: str) -> Callable[[Json], Any]:
        return lambda w: (w.get("retail_prices") or {}).get(site)

    def reasoning(path: str) -> Callable[[Json], Any]:
        return lambda w: (w.get("reasoning_summary") or {}).get(path)

    head = ["row_id", "customer_part_no_raw", "customer_part_no", "our_part_no", "oe_status"]
    return [
        *[(name, _field(name)) for name in head],
        ("current_oe", _field("current_oe")),
        ("supersession_chain", _joined(" -> ", "supersession_chain")),
        ("oe_candidates", _joined(", ", "oe_candidates")),
        *[(name, _field(name)) for name in ("msrp", "selected_retail_price", "landed_cost")],
        ("msrp_status", _field("msrp_status")),
        *[(f"retail_price_{site}", site_price(site)) for site in sites],
        ("selected_retail_source", _field("selected_retail_source")),
        ("retail_outliers", _joined(", ", "retail_outliers")),
        ("should_cost_fob", _field("should_cost_fob")),
        ("shared_oe_rows", _joined(", ", "shared_oe_rows")),
        ("price_source_row", _field("price_source_row")),
        ("shared_prices_disagree", _field("shared_prices_disagree")),
        ("confidence_score", _field("confidence_score")),
        ("band", _field("band")),
        ("review_status", _field("review_status")),
        ("review_reasons", _joined("\n", "review_reasons")),
        ("failures", _joined("\n", "failures")),
        *[(f"reasoning_{path}", reasoning(path)) for path in REASONING_PATHS],
    ]


def worksheet_frame(items: Sequence[Json]) -> pd.DataFrame:
    """Worksheet rows (``RowPage.items``) as a table with the Excel sheet's columns."""
    sites = sorted({site for w in items for site in w.get("retail_prices") or {}})
    cols = _columns(sites)
    frame = pd.DataFrame(
        [[value(w) for _, value in cols] for w in items], columns=[name for name, _ in cols]
    )
    for name in INT_COLUMNS:  # keep integers that may be blank from turning into floats
        frame[name] = frame[name].astype("Int64")
    return frame


def band_colors(frame: pd.DataFrame) -> list[str | None]:
    return [BAND_COLORS.get(band) if isinstance(band, str) else None for band in frame["band"]]


def styled_worksheet(frame: pd.DataFrame) -> Styler:
    """The table with each row filled in its band's colour."""
    colors = band_colors(frame)

    def fill(row: pd.Series) -> list[str]:
        color = colors[row.name] if isinstance(row.name, int) else None
        return [f"background-color: {color}" if color else ""] * len(row)

    return frame.style.apply(fill, axis=1).format(precision=2, na_rep="—")


def price_chart(bars: Sequence[dict[str, Any]], msrp: float | None) -> AltairChart:
    """Effective price per site: the selected quote, outliers and other quotes; MSRP as a rule."""
    frame = pd.DataFrame([b for b in bars if b["effective_price"] is not None])
    roles = alt.Scale(domain=list(ROLE_COLORS), range=list(ROLE_COLORS.values()))
    chart = (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X("site:N", title="站点"),
            y=alt.Y("effective_price:Q", title="实际价（USD）"),
            color=alt.Color(
                "role:N", scale=roles, title="", legend=alt.Legend(labelExpr=ROLE_EXPR)
            ),
            tooltip=["site", "effective_price", "list_price", "sale_price", "in_stock", "role"],
        )
    )
    layers: list[alt.Chart] = [chart]
    if msrp is not None:
        rule = (
            alt.Chart(pd.DataFrame({"msrp": [msrp]}))
            .mark_rule(strokeDash=[6, 4])
            .encode(y="msrp:Q", tooltip=["msrp"])
        )
        layers.append(rule)
    return cast(AltairChart, alt.layer(*layers).properties(title="各站点价格"))


def waterfall_chart(breakdown: Json) -> AltairChart:
    frame = pd.DataFrame(waterfall(breakdown))
    order = list(frame["item"])
    chart = (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X("item:N", sort=order, title=""),
            y=alt.Y("start:Q", title="USD"),
            y2="end:Q",
            color=alt.Color(
                "kind:N",
                title="",
                scale=alt.Scale(
                    domain=list(WATERFALL_COLORS), range=list(WATERFALL_COLORS.values())
                ),
            ),
            tooltip=[
                "item",
                alt.Tooltip("start:Q", format=".2f"),
                alt.Tooltip("end:Q", format=".2f"),
            ],
        )
        .properties(title="成本瀑布")
    )
    return cast(AltairChart, chart)


def signals_chart(evidence: Sequence[tuple[str, float]]) -> AltairChart:
    frame = pd.DataFrame(evidence, columns=["signal", "value"])
    chart = (
        alt.Chart(frame)
        .mark_bar()
        .encode(
            x=alt.X("value:Q", scale=alt.Scale(domain=[0, 1]), title="信号（0–1）"),
            y=alt.Y("signal:N", sort=[name for name, _ in evidence], title=""),
            tooltip=["signal", "value"],
        )
        .properties(title="置信度信号")
    )
    return cast(AltairChart, chart)


# --- evaluation (spec 14) ---------------------------------------------------------------------

CURVE_METRICS = {"coverage": "覆盖率", "accuracy": "准确率"}
COMPARE_TARGET = 0.95


def _run_label(ev: Json) -> str:
    return f"{ev['run_id']} ({ev['strategy']})"


def reliability_points(evals: Sequence[Json]) -> list[dict[str, Any]]:
    """Non-empty score bins of each run: mean score against the share usable as is."""
    return [
        {
            "run": _run_label(ev),
            "mean_score": b["mean_score"],
            "usable_rate": b["usable_rate"],
            "rows": b["rows"],
        }
        for ev in evals
        for b in ev["calibration"]["reliability"]
        if b["rows"]
    ]


def curve_points(evals: Sequence[Json]) -> list[dict[str, Any]]:
    """Coverage and accuracy per threshold, long format; thresholds with no rows lack accuracy."""
    return [
        {"run": _run_label(ev), "threshold": p["threshold"], "metric": label, "value": p[key]}
        for ev in evals
        for p in ev["calibration"]["curve"]
        for key, label in CURVE_METRICS.items()
        if p[key] is not None
    ]


def _pick(ev: Json, target: float = COMPARE_TARGET) -> Json:
    """The run's lowest threshold reaching ``target`` accuracy (``{}`` if not evaluated)."""
    return next((p for p in ev["calibration"]["thresholds"] if p["target"] == target), {})


def threshold_marks(evals: Sequence[Json], target: float = COMPARE_TARGET) -> list[dict[str, Any]]:
    """Each run's lowest threshold reaching ``target`` accuracy, to mark on the curve."""
    marks = []
    for ev in evals:
        pick = _pick(ev, target)
        if pick.get("threshold") is not None:
            marks.append(
                {
                    "run": _run_label(ev),
                    "threshold": pick["threshold"],
                    "label": f"t={pick['threshold']} ({target:.0%})",
                }
            )
    return marks


def _injection_cells(ev: Json) -> dict[str, Any]:
    """Injection columns of the comparison; empty for a run without injected errors."""
    inj = ev.get("injection")
    return {
        "注入行被拦下": inj["held_back_rate"] if inj else None,
        "绿档注入行": f"{inj['green_injected']} / {inj['rows']}" if inj else None,
        "注入 AUROC": inj["auroc"] if inj else None,
    }


def compare_frame(evals: Sequence[Json]) -> pd.DataFrame:
    """One row per evaluated run: the numbers for choosing a strategy and thresholds."""
    records = []
    for ev in evals:
        cal, usage = ev["calibration"], ev["usage"]
        bands = cal["bands"]
        green = bands["green"]
        pick = _pick(ev)
        records.append(
            {
                "运行": ev["run_id"],
                "策略": ev["strategy"],
                "模型": ", ".join(f"{k} {v}" for k, v in sorted(usage["models"].items())),
                "行数": ev["rows"],
                "绿 / 黄 / 红": " / ".join(str(bands[b]["rows"]) for b in BAND_COLORS),
                "行正确率": cal["row_correct_rate"],
                "可直接使用率": cal["usable_rate"],
                "ECE": cal["ece"],
                "AUROC": cal["auroc"],
                "绿档可直接使用": green["usable"] / green["rows"] if green["rows"] else None,
                "红档行正确": cal["red_row_correct_rate"],
                f"{COMPARE_TARGET:.0%} 阈值": pick.get("threshold"),
                f"{COMPARE_TARGET:.0%} 覆盖率": pick.get("coverage"),
                **_injection_cells(ev),
                "本运行调用": sum(a["calls"] for a in usage["agents"]),
                "本运行成本": usage["cost_usd"],
                "合计成本": usage["total_cost_usd"],
                "每千行成本": usage["cost_per_1k_rows"],
                "耗时（秒）": usage["elapsed_s"],
            }
        )
    return pd.DataFrame(records)


def reliability_chart(points: Sequence[dict[str, Any]]) -> AltairChart:
    """Reliability diagram: one line per run, the diagonal as perfect calibration."""
    diagonal = (
        alt.Chart(pd.DataFrame({"x": [0.0, 1.0], "y": [0.0, 1.0]}))
        .mark_line(strokeDash=[6, 4], color="grey")
        .encode(x="x:Q", y="y:Q")
    )
    lines = (
        alt.Chart(pd.DataFrame(points))
        .mark_line(point=True)
        .encode(
            x=alt.X("mean_score:Q", scale=alt.Scale(domain=[0, 1]), title="平均分数 / 100"),
            y=alt.Y("usable_rate:Q", scale=alt.Scale(domain=[0, 1]), title="可直接使用比例"),
            color=alt.Color("run:N", title="运行"),
            tooltip=["run", "mean_score", "usable_rate", "rows"],
        )
    )
    return cast(AltairChart, alt.layer(diagonal, lines).properties(title="可靠性图"))


def coverage_chart(
    points: Sequence[dict[str, Any]],
    marks: Sequence[dict[str, Any]] = (),
    target: float = COMPARE_TARGET,
) -> AltairChart:
    """Coverage (solid) and accuracy (dashed) against the threshold.

    The target accuracy is a horizontal rule; ``marks`` (``threshold_marks``) are vertical
    rules at the threshold each run needs to reach it.
    """
    curves = (
        alt.Chart(pd.DataFrame(points))
        .mark_line()
        .encode(
            x=alt.X(
                "threshold:Q", scale=alt.Scale(domain=[0, 100]), title="阈值（分数 ≥ t 自动处理）"
            ),
            y=alt.Y("value:Q", scale=alt.Scale(domain=[0, 1]), title="比例"),
            color=alt.Color("run:N", title="运行"),
            strokeDash=alt.StrokeDash("metric:N", title=""),
            tooltip=["run", "metric", "threshold", "value"],
        )
    )
    rule = (
        alt.Chart(pd.DataFrame({"target": [target]}))
        .mark_rule(color="grey", strokeDash=[2, 2])
        .encode(y="target:Q")
    )
    layers: list[alt.Chart] = [curves, rule]
    if marks:
        picked = alt.Chart(pd.DataFrame(marks)).encode(
            x="threshold:Q", color=alt.Color("run:N", title="运行"), tooltip=["run", "label"]
        )
        layers.append(picked.mark_rule(strokeDash=[4, 2]))
        layers.append(picked.mark_text(align="left", dx=4).encode(y=alt.value(12), text="label:N"))
    return cast(AltairChart, alt.layer(*layers).properties(title="覆盖率-准确率曲线"))
