"""Page 2, run overview: the run summary of spec 12.3 (``RunDetail.summary``)."""

from __future__ import annotations

from tests.dashboard.conftest import Backend, open_page, run_detail, stub_api, texts


def _metrics(at: object) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}  # type: ignore[attr-defined]


def test_a_finished_run_shows_its_counts_coverage_cost_and_time(backend: Backend) -> None:
    run_id = backend.finished_run()
    summary = backend.api.run(run_id)["summary"]
    at = open_page("overview", backend.api, run_id=run_id)
    metrics = _metrics(at)
    assert metrics["总行数"] == "20"
    bands = summary["bands"]
    assert (metrics["绿档"], metrics["黄档"], metrics["红档"]) == (
        str(bands["green"]),
        str(bands["yellow"]),
        str(bands["red"]),
    )
    assert metrics["待复核"] == str(summary["review_pending"])
    assert metrics["OE 覆盖率"] == f"{summary['coverage']['oe']:.0%}"
    assert metrics["LLM 调用"] == str(summary["llm_calls"])
    assert metrics["LLM 成本"].startswith("$")
    assert metrics["耗时"].endswith("s")
    shown = texts(at)
    assert "墙钟" in shown
    assert "evidence" in shown  # the run's strategy (RunSpec)


def test_an_unpriced_model_shows_no_cost() -> None:
    detail = run_detail("completed", done=3)
    detail["summary"] = {
        "rows": 2,
        "bands": {"green": 2},
        "reviews": {},
        "review_pending": 0,
        "coverage": {"oe": 1.0, "msrp": 0.5, "retail": 0.5, "should_cost": 1.0},
        "rows_with_failure": 0,
        "llm_calls": 4,
        "input_tokens": 100,
        "output_tokens": 10,
        "llm_cost_usd": None,
        "elapsed_s": None,
    }
    api, _ = stub_api({("GET", "/runs/r1"): detail})
    metrics = _metrics(open_page("overview", api, run_id="r1"))
    assert metrics["LLM 成本"] == "—"
    assert metrics["红档"] == "0"


def test_a_run_not_finalized_yet_shows_its_progress_instead() -> None:
    api, _ = stub_api({("GET", "/runs/r1"): run_detail(done=1, total=3)})
    at = open_page("overview", api, run_id="r1")
    assert "收尾后" in texts(at)
    assert at.get("progress")[0].proto.value == 33


def test_without_a_selected_run_the_page_asks_for_one() -> None:
    api, _ = stub_api({})
    assert "先在侧栏选择一个运行" in texts(open_page("overview", api))
