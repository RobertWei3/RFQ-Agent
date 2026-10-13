"""Page 3: the worksheet, filtered and paged by the API, rows coloured by band."""

from __future__ import annotations

import math

import streamlit as st

from dashboard import charts
from dashboard.api_client import XLSX, ApiClient, ApiError
from dashboard.views import common

TITLE = "工作表"
PAGE_SIZES = [50, 100, 500]


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
    if detail["summary"] is not None:
        st.download_button(
            "下载工作表（xlsx）",
            data=lambda: api.worksheet(run_id),
            file_name=f"run_{run_id}.xlsx",
            mime=XLSX,
            on_click="ignore",
            key="ws_download",
        )
    else:
        st.info("运行还没有收尾，工作表中的行还不完整，收尾后才能下载。")

    left, middle, right = st.columns([2, 2, 1])
    bands = left.multiselect(
        "档位",
        list(common.BAND_LABELS),
        format_func=lambda band: common.BAND_LABELS[band],
        key="ws_bands",
    )
    statuses = middle.multiselect(
        "复核状态",
        list(common.REVIEW_LABELS),
        format_func=lambda status: common.REVIEW_LABELS[status],
        key="ws_statuses",
    )
    size = right.selectbox("每页行数", PAGE_SIZES, index=1, key="ws_size")

    page = st.session_state.get("ws_page", 1)
    try:
        result = api.rows(
            run_id, bands=bands, statuses=statuses, offset=(page - 1) * size, limit=size
        )
        pages = max(1, math.ceil(result["total"] / size))
        if page > pages:  # the filters shrank the list: back to its first page
            st.session_state["ws_page"] = page = 1
            result = api.rows(run_id, bands=bands, statuses=statuses, offset=0, limit=size)
    except ApiError as exc:
        common.show_error(exc)
        return

    st.number_input("页码", min_value=1, max_value=pages, step=1, key="ws_page")
    st.caption(f"共 {result['total']} 行，第 {page} / {pages} 页")
    frame = charts.worksheet_frame(result["items"])
    event = st.dataframe(
        charts.styled_worksheet(frame),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="ws_table",
    )
    chosen = event.selection.rows if event else []
    if chosen:
        st.session_state["ws_open_row"] = int(frame.iloc[chosen[0]]["row_id"])

    left, right = st.columns([1, 3], vertical_alignment="bottom")
    row_id = left.number_input("行号", min_value=1, value=None, step=1, key="ws_open_row")
    if right.button("打开行详情", key="ws_open", disabled=row_id is None):
        common.open_row(int(row_id or 0))
