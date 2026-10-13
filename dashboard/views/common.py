"""Pieces several pages share: the selected run, run status, polling, errors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

from dashboard.api_client import ApiClient, ApiError

POLL_S = 2
RUN_KEY = "run_id"


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def poll(body: Callable[[], None]) -> None:
    """Run ``body`` now and again every ``POLL_S`` seconds (a Streamlit fragment)."""
    st.fragment(run_every=POLL_S)(body)()


def show_error(exc: ApiError) -> None:
    st.error(f"API 返回 {exc.status}：{exc.detail}" if exc.status else exc.detail)


def selected_run() -> str | None:
    run_id: str | None = st.session_state.get(RUN_KEY)
    return run_id


def select_run(run_id: str) -> None:
    st.session_state[RUN_KEY] = run_id
    st.query_params["run"] = run_id


def run_picker(api: ApiClient) -> None:
    """Sidebar list of runs (newest first); the choice follows the ``?run=`` parameter."""
    if RUN_KEY not in st.session_state and "run" in st.query_params:
        st.session_state[RUN_KEY] = st.query_params["run"]
    try:
        runs = api.list_runs()
    except ApiError as exc:
        with st.sidebar:
            show_error(exc)
        return
    ids = [r["run_id"] for r in runs]
    # Labels must not change while a run is selected: the browser keeps the choice by label.
    labels = {
        r["run_id"]: f"{r['run_id']} · {r['created_at'][:19].replace('T', ' ')}" for r in runs
    }
    current = selected_run()
    if current and st.query_params.get("run") != current:  # navigation drops the parameter
        st.query_params["run"] = current
    if current in ids and st.session_state.get("run_picker") != current:
        st.session_state["run_picker"] = current
    st.sidebar.selectbox(
        "运行",
        ids,
        index=ids.index(current) if current in ids else None,
        format_func=lambda run_id: labels[run_id],
        key="run_picker",
        placeholder="选择一个运行",
        on_change=_picked,
        args=(ids,),
    )


def _picked(ids: list[str]) -> None:
    choice = st.session_state["run_picker"]
    if choice in ids:  # ignore anything that is not a listed run
        select_run(choice)


def require_run() -> str | None:
    run_id = selected_run()
    if run_id is None:
        st.info("先在侧栏选择一个运行，或在“上传与运行”页上传询价单。")
    return run_id


def batches_done(progress: dict[str, int]) -> int:
    return progress["completed"] + progress["failed"]


def status_message(detail: dict[str, Any]) -> tuple[str, str]:
    """(level, text) describing the run's state as spec 13 defines it."""
    run, progress = detail["run"], detail["progress"]
    status = run["status"]
    if status == "awaiting_review":
        pending = (detail.get("summary") or {}).get("review_pending", 0)
        return "success", f"已收尾，工作表可下载，还有 {pending} 个红档行待复核"
    if status == "completed":
        return "success", "已完成，工作表可下载"
    if not detail["lease_active"]:
        return "warning", "后台已停止，可续跑"
    done, total = batches_done(progress), progress["batches_total"]
    if done == total:
        return "info", "所有批已完成，收尾中"
    return "info", f"运行中：已完成 {done} / {total} 批"


def show_status(api: ApiClient, detail: dict[str, Any]) -> None:
    """Progress bar, state message and, for a stopped run, the resume button."""
    progress = detail["progress"]
    total = progress["batches_total"]
    st.progress(round(100 * batches_done(progress) / total) if total else 0)
    st.caption(
        f"批：共 {total}，完成 {progress['completed']}，失败 {progress['failed']}，"
        f"运行中 {progress['running']}，待运行 {progress['pending']}"
    )
    level, text = status_message(detail)
    getattr(st, level)(text)
    if detail["run"]["status"] == "running" and not detail["lease_active"]:
        run_id = detail["run"]["run_id"]
        if st.button("续跑", key="resume_run"):
            try:
                api.resume(run_id)
            except ApiError as exc:
                show_error(exc)
            else:
                st.rerun()


def run_status(api: ApiClient, run_id: str) -> None:
    """The run's status; polled every ``POLL_S`` seconds while a process drives it."""
    try:
        detail = api.run(run_id)
    except ApiError as exc:
        show_error(exc)
        return
    if detail["run"]["status"] != "running" or not detail["lease_active"]:
        show_status(api, detail)
        return

    def refresh() -> None:
        try:
            latest = api.run(run_id)
        except ApiError as exc:
            show_error(exc)
            return
        if latest["run"]["status"] != "running" or not latest["lease_active"]:
            st.rerun()  # whole page: stop polling and show the final state
        show_status(api, latest)

    poll(refresh)


BAND_LABELS = {"green": "绿", "yellow": "黄", "red": "红"}
REVIEW_LABELS = {
    "unreviewed": "未复核",
    "accepted": "已接受",
    "modified": "已修改",
    "rejected": "已拒绝",
}
ROW_KEY = "row_id"


def open_row(row_id: int) -> None:
    """Show ``row_id`` on the row detail page (switching to it inside the full app)."""
    st.session_state[ROW_KEY] = row_id
    pages = st.session_state.get("pages")
    if pages:
        st.switch_page(pages["row"])
