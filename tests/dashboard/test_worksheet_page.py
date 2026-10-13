"""Page 3, worksheet: band / review-status filters and paging done by the API (spec 15.2)."""

from __future__ import annotations

import httpx

from tests.dashboard.conftest import Backend, open_page, run_detail, stub_api, texts


def test_the_worksheet_lists_every_row_and_filters_by_band_and_review_status(
    backend: Backend,
) -> None:
    run_id = backend.finished_run()
    at = open_page("worksheet", backend.api, run_id=run_id)
    table = at.dataframe[0].value
    assert list(table["row_id"]) == list(range(1, 21))
    assert "retail_price_retail_1" in table.columns
    assert "共 20 行" in texts(at)

    at.multiselect(key="ws_bands").set_value(["red"]).run()
    red = at.dataframe[0].value
    assert len(red) > 0 and set(red["band"]) == {"red"}

    at.multiselect(key="ws_statuses").set_value(["accepted"]).run()
    assert at.dataframe[0].value.empty
    assert "共 0 行" in texts(at)


def test_the_worksheet_downloads_once_the_run_is_finalized(backend: Backend) -> None:
    run_id = backend.finished_run()
    at = open_page("worksheet", backend.api, run_id=run_id)
    assert [b.label for b in at.get("download_button")] == ["下载工作表（xlsx）"]


def _page_route(seen_offsets: list[str]) -> object:
    def answer(request: httpx.Request) -> httpx.Response:
        seen_offsets.append(request.url.params["offset"])
        return httpx.Response(200, json={"total": 250, "offset": 0, "limit": 100, "items": []})

    return answer


def test_pages_are_fetched_from_the_api_by_offset() -> None:
    offsets: list[str] = []
    api, _ = stub_api(
        {("GET", "/runs/r1"): run_detail(done=1), ("GET", "/runs/r1/rows"): _page_route(offsets)}
    )
    at = open_page("worksheet", api, run_id="r1")
    assert "共 250 行" in texts(at)
    at.number_input(key="ws_page").set_value(3).run()
    assert offsets == ["0", "200"]
    # not finalized: no worksheet to download yet
    assert at.get("download_button") == []


def test_a_row_can_be_opened_in_the_row_detail_page(backend: Backend) -> None:
    run_id = backend.finished_run()
    at = open_page("worksheet", backend.api, run_id=run_id)
    at.number_input(key="ws_open_row").set_value(5).run()
    at.button(key="ws_open").click().run()
    assert at.session_state["row_id"] == 5
