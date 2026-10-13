"""Page 2: run overview — rows, bands, reviews, coverage, LLM usage and time (spec 12.3)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.views import common

TITLE = "运行总览"
COVERAGE = {"oe": "OE", "msrp": "MSRP", "retail": "零售价", "should_cost": "应有成本"}


def render(api: ApiClient) -> None:
    st.title(TITLE)
    run_id = common.require_run()
    if run_id is None:
        return
    try:
        detail = api.run(run_id)
    except ApiError as exc:
        common.show_error(exc)
        return
    st.subheader(f"运行 {run_id}")
    summary = detail["summary"]
    if summary is None:
        st.info("运行收尾后才有汇总，当前进度如下。")
        common.run_status(api, run_id)
    else:
        level, text = common.status_message(detail)
        getattr(st, level)(text)
        _summary(summary)
    _spec(detail["run"])


def _summary(summary: dict[str, Any]) -> None:
    bands, reviews = summary["bands"], summary["reviews"]
    cols = st.columns(5)
    cols[0].metric("总行数", summary["rows"])
    cols[1].metric("绿档", bands.get("green", 0))
    cols[2].metric("黄档", bands.get("yellow", 0))
    cols[3].metric("红档", bands.get("red", 0))
    cols[4].metric("有行失败", summary["rows_with_failure"])

    st.markdown("**人工复核**")
    cols = st.columns(4)
    cols[0].metric("已接受", reviews.get("accepted", 0))
    cols[1].metric("已修改", reviews.get("modified", 0))
    cols[2].metric("已拒绝", reviews.get("rejected", 0))
    cols[3].metric("待复核", summary["review_pending"])

    st.markdown("**各阶段覆盖率**（按系统结果，不含人工改价）")
    cols = st.columns(len(COVERAGE))
    for col, (key, label) in zip(cols, COVERAGE.items(), strict=True):
        col.metric(f"{label} 覆盖率", f"{summary['coverage'][key]:.0%}")

    st.markdown("**LLM 与耗时**")
    cost, elapsed = summary["llm_cost_usd"], summary["elapsed_s"]
    cols = st.columns(4)
    cols[0].metric("LLM 调用", summary["llm_calls"])
    cols[1].metric(
        "Token（输入 / 输出）", f"{summary['input_tokens']} / {summary['output_tokens']}"
    )
    cols[2].metric("LLM 成本", "—" if cost is None else f"${cost:.4f}")
    cols[3].metric("耗时", "—" if elapsed is None else f"{elapsed:.1f}s")
    if cost is None:
        st.caption("LLM 成本为空：有未定价的模型。")
    st.caption("耗时为墙钟时间：从建运行到最近一次收尾完成，含进程被中断后的停机时间。")


def _spec(run: dict[str, Any]) -> None:
    spec = run["spec"]
    st.markdown("**运行参数**")
    st.markdown(
        f"- 置信度策略：`{spec['strategy']}`\n"
        f"- 批大小：{spec['batch_size']}\n"
        f"- 人工复核：{'开启' if spec['review'] else '未开启'}\n"
        f"- 创建时间：{run['created_at']}，最近收尾：{run.get('finished_at') or '—'}\n"
        f"- 询价单：`{spec['input']}`"
    )
