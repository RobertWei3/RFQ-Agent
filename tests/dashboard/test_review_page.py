"""Page 5, review queue: answer red rows; a new OE number finishes in a worker (spec 15.2)."""

from __future__ import annotations

import json
import time

import httpx
from streamlit.testing.v1 import AppTest

from dashboard.views import review
from tests.dashboard.conftest import Backend, open_page, run_detail, stub_api, texts


def _queue(backend: Backend, run_id: str) -> list[int]:
    page = backend.api.rows(run_id, bands=["red"], statuses=["unreviewed"], limit=1000)
    return [w["row_id"] for w in page["items"]]


def _answer(at: AppTest, row_id: int, action: str, **fields: object) -> AppTest:
    at.selectbox(key="rq_row").set_value(row_id).run()
    at.radio(key="rq_action").set_value(action).run()
    for key, value in fields.items():
        widget = at.text_input(key=key) if key in ("rq_oe", "rq_note") else at.number_input(key=key)
        widget.set_value(value)
    return at.button(key="rq_submit").click().run()


def test_red_rows_are_accepted_repriced_and_given_a_new_oe_number(backend: Backend) -> None:
    run_id = backend.finished_run()
    queue = _queue(backend, run_id)
    assert len(queue) >= 3
    at = open_page("review", backend.api, run_id=run_id)
    assert at.selectbox(key="rq_row").options == [str(r) for r in queue]

    first, second, third = queue[:3]
    _answer(at, first, "accept", rq_note="looks fine")
    assert backend.api.row(run_id, first)["row"]["worksheet"]["review_status"] == "accepted"
    assert f"第 {first} 行" in texts(at)
    at.run()  # a browser may rerun once more after the click: the notice stays
    assert f"第 {first} 行：已接受" in texts(at)
    assert str(first) not in at.selectbox(key="rq_row").options
    assert at.text_input(key="rq_note").value == ""  # the form starts over for the next row

    _answer(at, second, "modify_price", rq_msrp=123.45)
    priced = backend.api.row(run_id, second)["row"]["worksheet"]
    assert (priced["review_status"], priced["msrp"]) == ("modified", 123.45)

    _answer(at, third, "modify_oe", rq_oe="NEW-OE-1")
    assert "后台处理中" in texts(at)
    assert at.radio(key="rq_action").value == "accept"
    assert "rq_oe" not in [w.key for w in at.text_input]
    backend.launcher.join()
    at.run()  # the next poll sees the worker's result
    shown = texts(at)
    assert "后台处理中" not in shown
    assert f"第 {third} 行" in shown and "已完成" in shown
    redone = backend.api.row(run_id, third)["row"]
    assert redone["worksheet"]["review_status"] == "modified"
    assert redone["oe"]["current_oe"] == "NEW-OE-1"


def test_a_run_without_review_mode_has_no_queue(backend: Backend) -> None:
    run_id = backend.finished_run(review=False)
    at = open_page("review", backend.api, run_id=run_id)
    assert "未开启复核" in texts(at)
    assert at.get("selectbox") == []


def test_a_change_without_its_value_is_not_sent(backend: Backend) -> None:
    run_id = backend.finished_run()
    at = open_page("review", backend.api, run_id=run_id)
    _answer(at, _queue(backend, run_id)[0], "modify_oe")
    assert "请填写新的 OE 号" in texts(at)
    assert backend.api.row(run_id, _queue(backend, run_id)[0])["in_review_queue"]


def _awaiting(row_id: int) -> dict[str, object]:
    detail = run_detail("awaiting_review", done=3, total=3)
    detail["summary"] = None
    page = {"total": 1, "offset": 0, "limit": 1000, "items": [{"row_id": row_id}]}
    return {"detail": detail, "page": page}


def _pending_row(row_id: int) -> dict[str, object]:
    worksheet = {"row_id": row_id, "review_status": None, "customer_part_no_raw": "CP"}
    return {"row": {"worksheet": worksheet}, "reviews": [], "in_review_queue": True}


def test_a_refused_review_shows_why() -> None:
    data = _awaiting(4)
    refuse = httpx.Response(409, json={"detail": "row 4 is not in the review queue"})
    api, _ = stub_api(
        {
            ("GET", "/runs/r1"): data["detail"],
            ("GET", "/runs/r1/rows"): data["page"],
            ("POST", "/runs/r1/rows/4/review"): lambda r: refuse,
        }
    )
    at = open_page("review", api, run_id="r1")
    at.button(key="rq_submit").click().run()
    assert "row 4 is not in the review queue" in texts(at)


def test_a_new_oe_number_whose_worker_stopped_can_be_sent_again() -> None:
    data = _awaiting(4)
    stopped = run_detail("awaiting_review", lease_active=False, done=3, total=3)
    posts: list[dict[str, object]] = []

    def accept_again(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1", "row_id": 4, "done": False, "row": None})

    api, _ = stub_api(
        {
            ("GET", "/runs/r1"): stopped,
            ("GET", "/runs/r1/rows"): data["page"],
            ("GET", "/runs/r1/rows/4"): _pending_row(4),
            ("POST", "/runs/r1/rows/4/review"): accept_again,
        }
    )
    pending = review.PendingOE("r1", 4, "N-1", None, time.time())
    at = open_page("review", api, run_id="r1", rq_pending=pending)
    assert "后台已停止" in texts(at)
    at.button(key="rq_resend").click().run()
    assert posts == [{"action": "modify_oe", "oe": "N-1"}]


def test_a_new_oe_number_still_running_after_the_limit_says_so() -> None:
    data = _awaiting(4)
    api, _ = stub_api(
        {
            ("GET", "/runs/r1"): run_detail("awaiting_review", done=3, total=3),
            ("GET", "/runs/r1/rows"): data["page"],
            ("GET", "/runs/r1/rows/4"): _pending_row(4),
        }
    )
    pending = review.PendingOE("r1", 4, "N-1", None, time.time() - 200)
    at = open_page("review", api, run_id="r1", rq_pending=pending)
    assert "仍在后台处理" in texts(at)


def test_a_worker_finishing_between_the_two_reads_is_not_reported_as_stopped() -> None:
    """The lease is read before the row: a worker that finishes in between counts as done."""
    data = _awaiting(4)
    row = _pending_row(4)
    run_reads = []

    def run(request: httpx.Request) -> httpx.Response:
        run_reads.append(request.url.path)
        if len(run_reads) == 2:  # the pending check's read: the worker has just finished
            row["row"]["worksheet"]["review_status"] = "modified"  # type: ignore[index]
            return httpx.Response(200, json=run_detail("awaiting_review", lease_active=False))
        return httpx.Response(200, json=data["detail"])

    api, _ = stub_api(
        {
            ("GET", "/runs/r1"): run,
            ("GET", "/runs/r1/rows"): data["page"],
            ("GET", "/runs/r1/rows/4"): lambda r: httpx.Response(200, json=row),
        }
    )
    pending = review.PendingOE("r1", 4, "N-1", None, time.time())
    at = open_page("review", api, run_id="r1", rq_pending=pending)
    shown = texts(at)
    assert "后台已停止" not in shown
    assert "第 4 行：改 OE 号已完成" in shown


def test_a_pending_new_oe_number_stops_polling_once_it_is_settled() -> None:
    """Only "waiting" keeps the 2 s poll going; every other phase is shown once, statically."""
    phase = review.pending_phase
    assert phase("modified", driven=False, waited_s=5) == "done"
    assert phase(None, driven=False, waited_s=5) == "stopped"
    assert phase(None, driven=True, waited_s=review.PENDING_LIMIT_S + 1) == "timed_out"
    assert phase(None, driven=True, waited_s=5) == "waiting"
