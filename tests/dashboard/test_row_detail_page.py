"""Page 4, row detail: chain, site prices, cost waterfall, signals, reasoning (spec 15.2)."""

from __future__ import annotations

import json
from typing import Any

from streamlit.testing.v1 import AppTest

from tests.dashboard.conftest import Backend, open_page, stub_api, texts


def _chart_titles(at: AppTest) -> list[str]:
    titles = []
    for chart in at.get("vega_lite_chart"):
        title = json.loads(chart.proto.spec).get("title")
        titles.append(title if isinstance(title, str) else title["text"])
    return titles


def _first(backend: Backend, run_id: str, check: Any) -> dict[str, Any]:
    items = backend.api.rows(run_id, limit=1000)["items"]
    return next(w for w in items if check(w))


def test_a_priced_row_shows_its_chain_prices_costs_signals_and_reasoning(
    backend: Backend,
) -> None:
    run_id = backend.finished_run()
    w = _first(
        backend,
        run_id,
        lambda w: (
            len(w["supersession_chain"]) >= 2 and w["selected_retail_source"] and w["landed_cost"]
        ),
    )
    at = open_page("row", backend.api, run_id=run_id, row_id=w["row_id"])
    shown = texts(at)
    *older, current = w["supersession_chain"]
    assert " → ".join([*older, f"**{current}**"]) in shown  # the current OE stands out
    assert set(_chart_titles(at)) == {"各站点价格", "成本瀑布", "置信度信号"}
    detail = backend.api.row(run_id, w["row_id"])["row"]
    assert detail["oe"]["reasoning"][:40] in shown
    assert detail["confidence"]["explanation"][:40] in shown
    assert f"{w['confidence_score']}" in shown


def test_a_row_with_several_oe_candidates_lists_them(backend: Backend) -> None:
    run_id = backend.finished_run()
    w = _first(backend, run_id, lambda w: w["oe_status"] == "multiple")
    at = open_page("row", backend.api, run_id=run_id, row_id=w["row_id"])
    candidates = at.dataframe[0].value
    assert set(w["oe_candidates"]) <= set(candidates["oe_part_no"])


def test_a_stage_that_failed_shows_its_stage_and_error(backend: Backend) -> None:
    run_id = backend.finished_run()
    row = backend.api.row(run_id, 1)
    row["row"]["retail"] = {"row_id": 1, "stage": "search_site", "error": "all sites timed out"}
    api, _ = stub_api({("GET", f"/runs/{run_id}/rows/1"): row})
    shown = texts(open_page("row", api, run_id=run_id, row_id=1))
    assert "search_site" in shown and "all sites timed out" in shown


def test_an_unknown_row_shows_the_api_error() -> None:
    api, _ = stub_api({})
    at = open_page("row", api, run_id="r1", row_id=99)
    assert "404" in texts(at)
