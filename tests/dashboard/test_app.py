"""The whole dashboard: six pages, the sidebar run picker, the ``?run=`` parameter."""

from __future__ import annotations

from pathlib import Path

import httpx
from streamlit.testing.v1 import AppTest

from tests.dashboard.conftest import TIMEOUT, Backend, run_detail, stub_api, texts

APP = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"


def _app(backend: Backend, **params: str) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=TIMEOUT)
    at.session_state["api_client"] = backend.api
    for key, value in params.items():
        at.query_params[key] = value
    return at.run()


def test_the_app_opens_on_upload_and_offers_every_run_in_the_sidebar(backend: Backend) -> None:
    first = backend.finished_run()
    second = backend.finished_run(review=False)
    at = _app(backend)
    assert at.title[0].value == "上传与运行"
    picker = at.sidebar.selectbox(key="run_picker")
    assert picker.value is None
    assert [o.split(" · ")[0] for o in picker.options] == [second, first]

    picker.set_value(first).run()
    assert at.session_state["run_id"] == first
    assert at.query_params["run"] == first


def test_a_run_in_the_url_is_selected(backend: Backend) -> None:
    run_id = backend.finished_run()
    at = _app(backend, run=run_id)
    assert at.sidebar.selectbox(key="run_picker").value == run_id
    assert f"运行 {run_id}" in texts(at)


def test_the_navigation_has_the_six_pages_of_the_spec(backend: Backend) -> None:
    # AppTest.switch_page only reaches file pages; switching between these function pages
    # (e.g. worksheet -> row detail) is checked in the browser walkthrough.
    at = _app(backend)
    pages = at.session_state["pages"]
    assert [p.title for p in pages.values()] == [
        "上传与运行",
        "运行总览",
        "工作表",
        "行详情",
        "复核队列",
        "评估",
    ]


def test_an_uploaded_run_is_selected_in_the_sidebar_at_once(backend: Backend) -> None:
    at = _app(backend)
    at.file_uploader[0].upload("rfq_sample_20.xlsx", backend.sample, "application/octet-stream")
    at.run()
    at.button(key="upload_start").click().run()
    run_id = at.session_state["run_id"]
    assert at.sidebar.selectbox(key="run_picker").value == run_id
    at.run()  # a browser may rerun once more after the click: the notice stays
    assert f"已创建运行 {run_id}" in texts(at)
    backend.launcher.join()


def test_the_selected_run_stays_in_the_url(backend: Backend) -> None:
    run_id = backend.finished_run()
    at = AppTest.from_file(str(APP), default_timeout=TIMEOUT)
    at.session_state["api_client"] = backend.api
    at.session_state["run_id"] = run_id  # chosen earlier; the URL lost it on navigation
    at.run()
    assert at.query_params["run"] == run_id


def test_the_selected_run_survives_a_change_of_its_status() -> None:
    record = run_detail()["run"]
    runs = [dict(record, status="running")]
    api, _ = stub_api({("GET", "/runs"): lambda r: httpx.Response(200, json=runs)})
    at = AppTest.from_file(str(APP), default_timeout=TIMEOUT)
    at.session_state["api_client"] = api
    at.run()
    at.sidebar.selectbox(key="run_picker").set_value("r1").run()
    runs[0] = dict(record, status="awaiting_review")  # the run finished meanwhile
    at.run()
    assert at.session_state["run_id"] == "r1"
    picker = at.sidebar.selectbox(key="run_picker")
    assert picker.value == "r1"
    # The browser keeps the choice by its label: a label that changed with the status lost it
    # (found in the browser walkthrough; AppTest does not reproduce the frontend's side).
    assert picker.options == ["r1 · 2026-10-08 00:00:00"]
