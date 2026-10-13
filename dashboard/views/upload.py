"""Page 1: upload an RFQ workbook and follow the run's progress."""

from __future__ import annotations

import streamlit as st

from dashboard.api_client import ApiClient, ApiError
from dashboard.views import common

TITLE = "上传与运行"
SERVER_DEFAULT = "（服务端默认）"
STRATEGIES = [SERVER_DEFAULT, "evidence", "evidence_plus_self", "evidence_plus_judge"]
ERROR_KEY = "upload_error"
CREATED_KEY = "upload_created"


def render(api: ApiClient) -> None:
    st.title(TITLE)
    upload = st.file_uploader("询价单（.xlsx）", type=["xlsx"], key="upload_file")
    left, right = st.columns(2)
    left.selectbox("置信度策略", STRATEGIES, key="upload_strategy")
    left.number_input(
        "批大小", min_value=1, value=None, step=1, key="upload_batch_size", placeholder="服务端默认"
    )
    right.number_input(
        "只处理前 N 行", min_value=1, value=None, step=1, key="upload_limit", placeholder="全部"
    )
    right.checkbox("开启人工复核", value=True, key="upload_review")
    st.button(
        "开始运行",
        key="upload_start",
        type="primary",
        disabled=upload is None,
        on_click=_start,
        args=(api,),
    )
    if error := st.session_state.pop(ERROR_KEY, None):
        common.show_error(error)
        if error.status == 409:
            st.info("服务端配置的是真实 LLM：需要用 `rfq api serve --allow-real` 启动服务端。")
    created = st.session_state.get(CREATED_KEY)
    if created and created == common.selected_run():
        st.success(f"已创建运行 {created}")
    run_id = common.selected_run()
    if run_id:
        st.subheader(f"运行 {run_id}")
        common.run_status(api, run_id)


def _start(api: ApiClient) -> None:
    """Button callback: runs before the page reruns, so the sidebar already lists the run."""
    state = st.session_state
    upload = state["upload_file"]
    strategy, batch_size, limit = (
        state["upload_strategy"],
        state["upload_batch_size"],
        state["upload_limit"],
    )
    try:
        record = api.create_run(
            upload.name,
            upload.getvalue(),
            strategy=None if strategy == SERVER_DEFAULT else strategy,
            batch_size=int(batch_size) if batch_size else None,
            review=state["upload_review"],
            limit=int(limit) if limit else None,
        )
    except ApiError as exc:
        state[ERROR_KEY] = exc
        return
    common.select_run(record["run_id"])
    state[CREATED_KEY] = record["run_id"]
