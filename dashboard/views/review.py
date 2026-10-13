"""Page 5: the review queue — accept, reject, reprice, or give a red row a new OE number."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Literal

import streamlit as st

from dashboard.api_client import ApiClient, ApiError, Json
from dashboard.views import common

TITLE = "复核队列"
ACTIONS = {
    "accept": "接受",
    "reject": "拒绝",
    "modify_price": "改价",
    "modify_oe": "改 OE 号",
}
PENDING_LIMIT_S = 120
PAGE_LIMIT = 1000

# Session-state keys: the form's widgets, then the page's own state.
ROW, ACTION, OE, MSRP, RETAIL, NOTE = (
    "rq_row",
    "rq_action",
    "rq_oe",
    "rq_msrp",
    "rq_retail",
    "rq_note",
)
FORM_RESET = {ACTION: "accept", OE: "", MSRP: None, RETAIL: None, NOTE: ""}
PENDING_KEY = "rq_pending"
NOTICE_KEY = "rq_notice"
ERROR_KEY = "rq_error"

Phase = Literal["done", "stopped", "timed_out", "waiting"]


@dataclass(frozen=True)
class PendingOE:
    """A new OE number the API accepted (202); a worker is applying it."""

    run_id: str
    row_id: int
    oe: str
    note: str | None
    since: float


def pending_phase(review_status: str | None, *, driven: bool, waited_s: float) -> Phase:
    """Where a pending new OE number stands; only ``waiting`` keeps polling."""
    if review_status == "modified":
        return "done"
    if not driven:
        return "stopped"
    if waited_s > PENDING_LIMIT_S:
        return "timed_out"
    return "waiting"


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
    notice = st.session_state.get(NOTICE_KEY)
    if notice and notice["run_id"] == run_id:  # the latest outcome, kept until the next one
        st.success(notice["text"])
    pending: PendingOE | None = st.session_state.get(PENDING_KEY)
    if pending and pending.run_id == run_id:
        _pending(api, pending)
    if not detail["run"]["spec"]["review"]:
        st.info("该运行未开启复核：建运行时勾选“开启人工复核”的运行才能复核。")
        return
    status = detail["run"]["status"]
    if status != "awaiting_review":
        reason = "运行还没有收尾" if status == "running" else "运行已完成，没有待复核的行"
        st.info(f"现在不能复核：{reason}。")
        return
    try:
        queue = _queue(api, run_id)
    except ApiError as exc:
        common.show_error(exc)
        return
    st.caption(f"待复核红档行：{len(queue)}")
    if not queue:
        st.info("队列已空。")
        return
    _form(api, run_id, queue)


def _queue(api: ApiClient, run_id: str) -> list[Json]:
    """Red rows not reviewed yet (spec 15.1 filters), every page of them."""
    items: list[Json] = []
    while True:
        page = api.rows(
            run_id, bands=["red"], statuses=["unreviewed"], offset=len(items), limit=PAGE_LIMIT
        )
        items += page["items"]
        if not page["items"] or len(items) >= page["total"]:
            return items


def _form(api: ApiClient, run_id: str, queue: list[Json]) -> None:
    rows = {w["row_id"]: w for w in queue}
    row_id = st.selectbox("待复核行", list(rows), key=ROW)
    _summary(rows[row_id])
    if st.button("在行详情中查看", key="rq_detail"):
        common.open_row(row_id)
    action = st.radio(
        "复核决定", list(ACTIONS), format_func=lambda a: ACTIONS[a], horizontal=True, key=ACTION
    )
    if action == "modify_oe":
        st.text_input("新的 OE 号", key=OE)
        st.caption("改 OE 号会对这一行重新查价和估算应有成本，由后台完成。")
    if action == "modify_price":
        left, right = st.columns(2)
        left.number_input("MSRP（USD）", min_value=0.01, value=None, key=MSRP)
        right.number_input("参考零售价（USD）", min_value=0.01, value=None, key=RETAIL)
        st.caption("改价只覆盖工作表上的数值，不重跑 agent、不重算置信度。")
    st.text_input("备注", key=NOTE)
    st.button(
        "提交复核", key="rq_submit", type="primary", on_click=_submit, args=(api, run_id, row_id)
    )
    if error := st.session_state.pop(ERROR_KEY, None):
        if isinstance(error, ApiError):
            common.show_error(error)
        else:
            st.error(error)


def _summary(w: Json) -> None:
    """A queued worksheet row at a glance."""
    lines = [f"客户件号 `{(w.get('customer_part_no_raw') or '').strip()}`"]
    if w.get("current_oe"):
        lines.append(f"当前 OE `{w['current_oe']}`")
    if w.get("oe_candidates"):
        lines.append("OE 候选 " + ", ".join(f"`{c}`" for c in w["oe_candidates"]))
    prices = [("MSRP", w.get("msrp")), ("参考零售价", w.get("selected_retail_price"))]
    prices.append(("到岸成本", w.get("landed_cost")))
    lines.append("；".join(f"{k} {common.money(v)}" for k, v in prices))
    if w.get("confidence_score") is not None:
        lines.append(f"置信度 {w['confidence_score']}")
    st.markdown(" · ".join(lines))
    reasons = w.get("review_reasons") or []
    if reasons:
        st.markdown("\n".join(f"- {r}" for r in reasons))


def _decision() -> dict[str, Any]:
    """The form's decision; only the fields its action uses."""
    state = st.session_state
    action = state[ACTION]
    decision: dict[str, Any] = {"action": action}
    if action == "modify_oe":
        decision["oe"] = (state.get(OE) or "").strip() or None
    if action == "modify_price":
        decision["msrp"] = state.get(MSRP)
        decision["retail"] = state.get(RETAIL)
    decision["note"] = (state.get(NOTE) or "").strip() or None
    return decision


def _missing(decision: dict[str, Any]) -> str | None:
    if decision["action"] == "modify_oe" and not decision.get("oe"):
        return "请填写新的 OE 号。"
    if decision["action"] == "modify_price" and not (
        decision.get("msrp") or decision.get("retail")
    ):
        return "改价至少要填 MSRP 或参考零售价中的一个（> 0）。"
    return None


def _submit(api: ApiClient, run_id: str, row_id: int) -> None:
    """Button callback: it runs before the page reruns, so the form can be reset."""
    decision = _decision()
    if problem := _missing(decision):
        st.session_state[ERROR_KEY] = problem
        return
    fields = {k: v for k, v in decision.items() if k != "action" and v is not None}
    try:
        outcome = api.review(run_id, row_id, decision["action"], **fields)
    except ApiError as exc:
        st.session_state[ERROR_KEY] = exc
        return
    if outcome["done"]:
        _notify(run_id, f"第 {row_id} 行：已{ACTIONS[decision['action']]}。")
    else:
        st.session_state[PENDING_KEY] = PendingOE(
            run_id, row_id, decision["oe"], decision["note"], time.time()
        )
    # Assign (not delete) so the browser's widgets are reset too; the answered row has left
    # the queue, so the row picker starts over.
    st.session_state.pop(ROW, None)
    st.session_state.update(FORM_RESET)


def _notify(run_id: str, text: str) -> None:
    st.session_state[NOTICE_KEY] = {"run_id": run_id, "text": text}


def _pending(api: ApiClient, pending: PendingOE) -> None:
    """A new OE number a worker is applying: poll every 2 s while it is still ``waiting``."""
    phase = _check(api, pending)
    if phase == "waiting":

        def poll() -> None:
            if _check(api, pending) != "waiting":
                st.rerun()  # whole page: show the outcome once and stop polling
            st.info(f"第 {pending.row_id} 行改 OE 号：后台处理中……")

        common.poll(poll)
    elif phase == "done":
        st.session_state.pop(PENDING_KEY, None)
        _notify(pending.run_id, f"第 {pending.row_id} 行：改 OE 号已完成，已重新查价与打分。")
        st.rerun()
    elif phase == "stopped":
        st.warning(
            f"第 {pending.row_id} 行改 OE 号的后台已停止，没有完成："
            "重新提交这一行的同一决定即可完成。"
        )
        if st.button("重新提交", key="rq_resend"):
            _resend(api, pending)
    elif phase == "timed_out":
        st.warning(f"第 {pending.row_id} 行改 OE 号仍在后台处理，可稍后刷新页面查看。")


def _check(api: ApiClient, pending: PendingOE) -> Phase | None:
    """The pending number's phase, or None after showing an API error."""
    try:
        # The lease first: a worker that finishes between the reads then shows as done.
        driven = api.run(pending.run_id)["lease_active"]
        row = api.row(pending.run_id, pending.row_id)["row"]
    except ApiError as exc:
        common.show_error(exc)
        return None
    status = row["worksheet"]["review_status"]
    return pending_phase(status, driven=driven, waited_s=time.time() - pending.since)


def _resend(api: ApiClient, pending: PendingOE) -> None:
    try:
        api.review(pending.run_id, pending.row_id, "modify_oe", oe=pending.oe, note=pending.note)
    except ApiError as exc:
        common.show_error(exc)
        return
    st.session_state[PENDING_KEY] = replace(pending, since=time.time())
    st.rerun()
