"""Page 1, upload and run: post an RFQ, then follow the run's progress (spec 15.2)."""

from __future__ import annotations

import httpx

from tests.dashboard.conftest import Backend, open_page, run_detail, stub_api, texts


def test_uploading_an_rfq_starts_a_run_and_shows_it_finishing(backend: Backend) -> None:
    at = open_page("upload", backend.api)
    assert at.checkbox(key="upload_review").value is True  # review is on by default
    at.file_uploader[0].upload("rfq_sample_20.xlsx", backend.sample, "application/octet-stream")
    at.number_input(key="upload_batch_size").set_value(7).run()
    at.button(key="upload_start").click().run()

    run_id = at.session_state["run_id"]
    runs = backend.api.list_runs()
    assert [r["run_id"] for r in runs] == [run_id]
    assert runs[0]["spec"]["review"] is True
    assert runs[0]["spec"]["batch_size"] == 7

    backend.launcher.join()
    at.run()  # the next poll sees the finished run
    shown = texts(at)
    assert "已收尾" in shown and "待复核" in shown
    assert at.get("progress")[0].proto.value == 100


def test_a_stopped_run_offers_resume() -> None:
    resumed = []

    def resume(request: httpx.Request) -> httpx.Response:
        resumed.append(request.url.path)
        return httpx.Response(202, json=run_detail()["run"])

    api, _ = stub_api(
        {
            ("GET", "/runs/r1"): run_detail(lease_active=False),
            ("POST", "/runs/r1/resume"): resume,
        }
    )
    at = open_page("upload", api, run_id="r1")
    assert "后台已停止，可续跑" in texts(at)
    at.button(key="resume_run").click().run()
    assert resumed == ["/runs/r1/resume"]


def test_a_run_whose_batches_are_done_is_finalizing() -> None:
    api, _ = stub_api({("GET", "/runs/r1"): run_detail(done=3, total=3)})
    at = open_page("upload", api, run_id="r1")
    assert "收尾中" in texts(at)


def test_a_real_provider_refusal_says_how_to_allow_it() -> None:
    detail = "real LLM provider for oe_interchange ~42 (expected calls); start the server ..."
    api, _ = stub_api({("POST", "/runs"): lambda r: httpx.Response(409, json={"detail": detail})})
    at = open_page("upload", api)
    at.file_uploader[0].upload("rfq.xlsx", b"PK", "application/octet-stream").run()
    at.button(key="upload_start").click().run()
    shown = texts(at)
    assert "oe_interchange ~42" in shown
    assert "--allow-real" in shown
    assert "run_id" not in at.session_state
