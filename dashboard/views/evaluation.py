"""Page 6: a run's evaluation against ground truth and the comparison of runs (spec 14).

The evaluation is made by ``rfq eval --run`` and read here over HTTP as plain JSON.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from dashboard import charts
from dashboard.api_client import ApiClient, ApiError, Json
from dashboard.views import common

TITLE = "评估"
COMPARE_KEY = "eval_compare"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def render(api: ApiClient) -> None:
    st.title(TITLE)
    run_id = common.require_run()
    if run_id is None:
        return
    try:
        ev = api.evaluation(run_id)
    except ApiError as exc:
        common.show_error(exc)
        return
    if ev is None:
        st.info(f"暂无评估：这个运行还没有评估结果。生成方法：`rfq eval --run {run_id}`")
        return
    _header(ev)
    _calibration(ev)
    _injection(ev)
    _stages(ev)
    _usage(ev)
    _compare(api, ev)


def _header(ev: Json) -> None:
    st.caption(
        f"策略 {ev['strategy']} · {ev['rows']} 行 · ground truth {ev['truth_path']} · "
        f"评估于 {ev['created_at']} · 按复核前系统结论评估的已复核行 {ev['reviewed_rows']}"
    )
    cal = ev["calibration"]
    cols = st.columns(4)
    cols[0].metric("行正确率", _pct(cal["row_correct_rate"]))
    cols[1].metric("可直接使用率", _pct(cal["usable_rate"]))
    cols[2].metric("ECE", _num(cal["ece"]))
    cols[3].metric("AUROC", _num(cal["auroc"]))


def _calibration(ev: Json) -> None:
    cal = ev["calibration"]
    st.subheader("置信度校准")
    st.caption("标签为“可直接使用”：行正确，且 OE 唯一确定、零售报价不是全部离群。")
    bands = pd.DataFrame(
        [
            {
                "档位": common.BAND_LABELS[band],
                "行数": b["rows"],
                "可直接使用": b["usable"],
                "行正确": b["row_correct"],
                "可直接使用比例": b["usable"] / b["rows"] if b["rows"] else None,
            }
            for band, b in cal["bands"].items()
        ]
    )
    st.dataframe(bands, hide_index=True)
    st.caption(f"红档中行正确的比例（被正确拦下的行）：{_pct(cal['red_row_correct_rate'])}")
    st.altair_chart(charts.reliability_chart(charts.reliability_points([ev])))
    st.altair_chart(charts.coverage_chart(charts.curve_points([ev]), charts.threshold_marks([ev])))
    picks = pd.DataFrame(
        [
            {"目标准确率": _pct(p["target"]), "最低阈值": p["threshold"], "覆盖率": p["coverage"]}
            for p in cal["thresholds"]
        ]
    )
    st.dataframe(picks, hide_index=True)


INJECTION_KINDS = {"oe": "OE", "msrp": "MSRP", "retail": "零售"}


def _injection(ev: Json) -> None:
    """Rows made wrong on purpose (spec 14 experiment): how many the scores held back."""
    inj = ev.get("injection")
    if not inj:
        return
    st.subheader("注入错误")
    st.caption(
        f"{inj['rows']} 行被人为改错。被拦下（落在黄档或红档）{_pct(inj['held_back_rate'])}，"
        f"留在绿档 {inj['green_injected']} 行，AUROC（可直接使用行 vs 注入行）{_num(inj['auroc'])}"
    )
    table = pd.DataFrame(
        [
            {
                "错误类型": INJECTION_KINDS[kind],
                **{common.BAND_LABELS[b]: n for b, n in bands.items()},
            }
            for kind, bands in inj["bands_by_kind"].items()
        ]
    )
    st.dataframe(table, hide_index=True)


def _stages(ev: Json) -> None:
    st.subheader("各阶段指标")
    oe, msrp, cost = ev["oe"], ev["msrp"], ev["should_cost"]
    rows: list[dict[str, Any]] = [
        {"阶段": "OE", "指标": "覆盖率（resolved）", "值": _pct(oe["coverage"])},
        {"阶段": "OE", "指标": "准确率（resolved 行）", "值": _pct(oe["accuracy"])},
        {"阶段": "OE", "指标": "结论正确率（全部行）", "值": _pct(oe["conclusion_accuracy"])},
        {"阶段": "OE", "指标": "替代链完全正确", "值": _pct(oe["chain_accuracy"])},
        {"阶段": "MSRP", "指标": "覆盖率", "值": _pct(msrp["coverage"])},
        {"阶段": "MSRP", "指标": "准确率（±1%，有价行）", "值": _pct(msrp["accuracy"])},
        {"阶段": "MSRP", "指标": "status 正确率", "值": _pct(msrp["status_accuracy"])},
        {"阶段": "MSRP", "指标": "平均相对误差", "值": _pct(msrp["mean_rel_error"])},
        {
            "阶段": "零售",
            "指标": "参考零售价站点正确率",
            "值": _pct(ev["retail"]["selected_accuracy"]),
        },
        {"阶段": "零售", "指标": "离群检测精确率", "值": _pct(ev["retail"]["outlier_precision"])},
        {"阶段": "零售", "指标": "离群检测召回率", "值": _pct(ev["retail"]["outlier_recall"])},
        {"阶段": "应有成本", "指标": "规格正确率", "值": _pct(cost["spec_accuracy"])},
        {
            "阶段": "应有成本",
            "指标": f"到岸成本 MAPE（零件 cycle time，n={cost['n_part']}）",
            "值": _pct(cost["landed_mape_part"]),
        },
        {
            "阶段": "应有成本",
            "指标": f"到岸成本 MAPE（品类默认 cycle time，n={cost['n_default']}）",
            "值": _pct(cost["landed_mape_default"]),
        },
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    sites = pd.DataFrame(
        [
            {
                "站点": name,
                "truth 有挂牌": s["expected"],
                "有报价": s["quoted"],
                "找到": s["found"],
                "覆盖率": s["coverage"],
            }
            for name, s in ev["retail"]["sites"].items()
        ]
    )
    st.dataframe(sites, hide_index=True)


def _usage(ev: Json) -> None:
    usage = ev["usage"]
    st.subheader("成本与耗时")
    if usage["agents"]:
        st.dataframe(pd.DataFrame(usage["agents"]).drop(columns="schema_version"), hide_index=True)
    source = usage["source_cost_usd"]
    st.caption(
        f"本运行 {common.money(usage['cost_usd'])}"
        + (f" · 源运行（agent）{common.money(source)}" if source is not None else "")
        + f" · 合计 {common.money(usage['total_cost_usd'])}"
        + f" · 每千行 {common.money(usage['cost_per_1k_rows'])}"
        + (f" · 耗时 {usage['elapsed_s']:.0f} 秒（墙钟）" if usage["elapsed_s"] is not None else "")
    )


def _compare(api: ApiClient, ev: Json) -> None:
    st.subheader("策略对比")
    try:
        runs = api.list_runs()
    except ApiError as exc:
        common.show_error(exc)
        return
    others = [r["run_id"] for r in runs if r["run_id"] != ev["run_id"]]
    picked = st.multiselect("与其他运行对比", others, key=COMPARE_KEY)
    evals = [ev]
    for run_id in picked:
        try:
            other = api.evaluation(run_id)
        except ApiError as exc:
            common.show_error(exc)
            continue
        if other is None:
            st.caption(f"{run_id}：未评估")
        else:
            evals.append(other)
    if len(evals) < 2:
        return
    st.dataframe(charts.compare_frame(evals), hide_index=True)
    st.altair_chart(charts.reliability_chart(charts.reliability_points(evals)))
    st.altair_chart(
        charts.coverage_chart(charts.curve_points(evals), charts.threshold_marks(evals))
    )
