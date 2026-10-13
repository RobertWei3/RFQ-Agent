"""Page 4: one row in full — chain, site prices, cost waterfall, signals, reasoning."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from dashboard import charts
from dashboard.api_client import ApiClient, ApiError
from dashboard.views import common

TITLE = "行详情"
STAGES = {"oe": "OE 解析", "msrp": "MSRP", "retail": "零售价", "should_cost": "应有成本"}


def render(api: ApiClient) -> None:
    st.title(TITLE)
    run_id = common.require_run()
    if run_id is None:
        return
    current = st.session_state.get(common.ROW_KEY)
    row_id = st.number_input("行号", min_value=1, value=current, step=1, placeholder="输入行号")
    if row_id is None:
        st.info("输入行号，或在工作表 / 复核队列中打开一行。")
        return
    st.session_state[common.ROW_KEY] = int(row_id)
    try:
        detail = api.row(run_id, int(row_id))
    except ApiError as exc:
        common.show_error(exc)
        return
    show_row(detail)


def show_row(detail: dict[str, Any]) -> None:
    """Every section of the row detail."""
    row = detail["row"]
    _conclusion(row["worksheet"], detail["in_review_queue"])
    _oe(row["oe"])
    _prices(row["msrp"], row["retail"], row["worksheet"])
    _cost(row["should_cost"])
    _confidence(row.get("confidence"))
    _reasoning(row)
    _reviews(detail["reviews"])


def _failure(result: dict[str, Any]) -> None:
    st.error(f"未查成：阶段 `{result['stage']}`，原因：{result['error']}")


def _conclusion(w: dict[str, Any], queued: bool) -> None:
    st.subheader(f"第 {w['row_id']} 行：{w['customer_part_no_raw'].strip()}")
    band = common.BAND_LABELS.get(w["band"] or "", "—")
    review = common.REVIEW_LABELS.get(w["review_status"] or "unreviewed")
    cols = st.columns(5)
    cols[0].metric("MSRP", common.money(w["msrp"]))
    source = w["selected_retail_source"]
    cols[1].metric(
        "参考零售价", common.money(w["selected_retail_price"]), source, delta_color="off"
    )
    cols[2].metric("到岸成本", common.money(w["landed_cost"]))
    cols[3].metric("置信度", "—" if w["confidence_score"] is None else w["confidence_score"])
    cols[4].metric("档位 / 复核", f"{band} / {review}")
    st.caption("参考价区间：MSRP、参考零售价、到岸成本并列，系统不给出最终报价。")
    if queued:
        st.warning("这一行在待复核队列中。")
    if w["review_reasons"]:
        st.markdown("**需复核原因**\n" + "\n".join(f"- {r}" for r in w["review_reasons"]))
    if w["failures"]:
        st.markdown("**行失败**\n" + "\n".join(f"- `{f}`" for f in w["failures"]))
    if w["shared_oe_rows"]:
        st.caption(
            f"与第 {', '.join(map(str, w['shared_oe_rows']))} 行共用 OE，"
            f"价格取自第 {w['price_source_row']} 行。"
        )


def _oe(oe: dict[str, Any]) -> None:
    st.markdown("### 替代链与 OE 候选")
    if charts.is_failure(oe):
        _failure(oe)
        return
    st.markdown(f"本公司件号：{oe['our_part_no'] or '—'}（来源 {oe['our_part_source']}）")
    st.markdown(f"OE 解析：**{oe['status']}**")
    chain = oe["supersession_chain"]
    if chain:
        st.markdown("替代链（旧 → 新）：")
        current = oe["current_oe"]
        st.markdown(" → ".join(f"**{n}**" if n == current else n for n in chain))
        st.caption(f"当前 OE：{oe['current_oe']}")
    if oe.get("chain_truncated"):
        st.warning("补链到跳数上限时仍有未追的号码（替代链可能不完整）。")
    if oe["candidates"]:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "oe_part_no": c["oe_part_no"],
                        "brand": c["brand"],
                        "match_type": c["match_type"],
                        "evidence": ", ".join(e["source"] for e in c["evidence"]),
                    }
                    for c in oe["candidates"]
                ]
            ),
            hide_index=True,
        )


def _prices(msrp: dict[str, Any], retail: dict[str, Any], w: dict[str, Any]) -> None:
    st.markdown("### 各站点价格")
    if charts.is_failure(msrp):
        _failure(msrp)
    else:
        quote = msrp.get("quote") or {}
        st.markdown(
            f"MSRP：**{msrp['status']}**，{common.money(quote.get('effective_price'))}"
            + (f"（价格取自 `{quote['matched_part_no']}`）" if quote else "")
        )
    if charts.is_failure(retail):
        _failure(retail)
        return
    bars = charts.site_prices(retail)
    if not bars:
        st.info("没有零售报价。")
        return
    st.altair_chart(charts.price_chart(bars, w["msrp"]))
    st.dataframe(pd.DataFrame(bars), hide_index=True)
    if retail["outliers"]:
        st.caption(f"离群站点：{', '.join(retail['outliers'])}")


def _cost(cost: dict[str, Any]) -> None:
    st.markdown("### 应有成本")
    if charts.is_failure(cost):
        _failure(cost)
        return
    weight = cost["weight_kg"]
    st.markdown(
        f"状态 **{cost['status']}**；品类 {cost['category'] or '—'}，"
        f"材质 {cost['material'] or '—'}，"
        f"重量 {'—' if weight is None else f'{weight} kg'}，"
        f"cycle time {cost['cycle_time_min'] or '—'} 分钟；规格来源 `{cost['spec_source']}`"
    )
    if cost["breakdown"]:
        st.altair_chart(charts.waterfall_chart(cost["breakdown"]))
    else:
        st.info("没有成本栈：数据不足，原因见下方应有成本的 reasoning。")


def _confidence(confidence: dict[str, Any] | None) -> None:
    st.markdown("### 置信度")
    if confidence is None:
        st.info("这一行还没有置信度。")
        return
    st.markdown(
        f"分数 **{confidence['score']}**，档位 {common.BAND_LABELS[confidence['band']]}，"
        f"策略 `{confidence['strategy']}`"
    )
    parts = charts.split_signals(confidence["signals"])
    if parts.evidence:
        st.altair_chart(charts.signals_chart(parts.evidence))
    if parts.gates:
        st.markdown(
            "**触发的封顶**\n"
            + "\n".join(f"- `{name}`：最高 {cap} 分" for name, cap in parts.gates)
        )
    if parts.llm:
        st.markdown("**LLM 信号**\n" + "\n".join(f"- `{name}`：{v:g}" for name, v in parts.llm))
    st.markdown(f"**分数构成**：{confidence['explanation']}")


def _reasoning(row: dict[str, Any]) -> None:
    st.markdown("### 各阶段 reasoning")
    for key, label in STAGES.items():
        result = row[key]
        with st.expander(label, expanded=False):
            if charts.is_failure(result):
                _failure(result)
                continue
            st.markdown(result.get("reasoning") or "（无）")
            evidence = _evidence(result)
            if evidence:
                st.markdown(
                    "\n".join(
                        f"- `{e['source']}` {e['ref']}"
                        + (f"：{e['snippet']}" if e["snippet"] else "")
                        for e in evidence
                    )
                )


def _evidence(result: dict[str, Any]) -> list[dict[str, Any]]:
    found = list(result.get("evidence") or [])
    for candidate in result.get("candidates") or []:
        found += candidate["evidence"]
    quotes = list(result.get("quotes") or [])
    if result.get("quote"):
        quotes.append(result["quote"])
    for quote in quotes:
        found += quote["evidence"]
    return found


def _reviews(reviews: list[dict[str, Any]]) -> None:
    if not reviews:
        return
    st.markdown("### 复核历史")
    for r in reviews:
        changes = [
            f"{name} {r[f'{name}_before']} → {r[f'{name}_after']}"
            for name in ("oe", "msrp", "retail")
            if r.get(f"{name}_after") is not None
        ]
        st.markdown(
            f"- {r['ts']} `{r['action']}` {'；'.join(changes)}"
            + (f"（备注：{r['note']}）" if r.get("note") else "")
        )
