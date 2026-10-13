"""Page 6, evaluation (spec 14, 15.2): metrics, calibration charts and strategy comparison."""

from __future__ import annotations

import json

import httpx

from rfq_agent.schemas import InjectionEval
from tests.builders import run_eval
from tests.dashboard.conftest import Backend, open_page, stub_api, texts


def test_a_run_without_an_evaluation_says_how_to_make_one(backend: Backend) -> None:
    run_id = backend.finished_run(review=False)
    at = open_page("evaluation", backend.api, run_id=run_id)
    shown = texts(at)
    assert "暂无评估" in shown and f"rfq eval --run {run_id}" in shown
    assert at.error == []


def test_an_evaluation_error_other_than_missing_is_shown() -> None:
    broken = httpx.Response(500, text="boom")
    api, _ = stub_api({("GET", "/runs/r1/eval"): lambda r: broken, ("GET", "/runs"): []})
    at = open_page("evaluation", api, run_id="r1")
    assert "500" in texts(at)


def test_an_evaluated_run_shows_metrics_bands_and_calibration_charts(backend: Backend) -> None:
    run_id = backend.finished_run(review=False)
    backend.evaluate(run_id)

    at = open_page("evaluation", backend.api, run_id=run_id)

    assert at.error == []
    shown = texts(at)
    for label in ("行正确率", "可直接使用率", "ECE", "AUROC"):
        assert label in shown, label
    assert "rfq_sample_20_ground_truth.jsonl" in shown
    bands = at.dataframe[0].value
    assert list(bands["档位"]) == ["绿", "黄", "红"]
    assert bands["行数"].sum() == 20
    titles = {json.loads(c.proto.spec).get("title") for c in at.get("vega_lite_chart")}
    assert {"可靠性图", "覆盖率-准确率曲线"} <= titles


def test_runs_can_be_compared_and_unevaluated_runs_are_flagged(backend: Backend) -> None:
    first = backend.finished_run(review=False)
    second = backend.finished_run(review=False)
    unevaluated = backend.finished_run(review=False)
    backend.evaluate(first)
    backend.evaluate(second)

    at = open_page("evaluation", backend.api, run_id=first)
    at.multiselect(key="eval_compare").set_value([second, unevaluated]).run()

    assert at.error == []
    table = next(d.value for d in at.dataframe if "运行" in d.value.columns)
    assert list(table["运行"]) == [first, second]
    assert f"{unevaluated}：未评估" in texts(at)


def test_an_injected_run_shows_how_many_wrong_rows_were_held_back() -> None:
    injection = InjectionEval(
        rows=3,
        bands_by_kind={
            "oe": {"green": 1, "yellow": 0, "red": 0},
            "msrp": {"green": 0, "yellow": 1, "red": 0},
            "retail": {"green": 0, "yellow": 0, "red": 1},
        },
        held_back_rate=2 / 3,
        green_injected=1,
        auroc=0.75,
    )
    ev = run_eval("r1").model_copy(update={"injection": injection}).model_dump(mode="json")
    api, _ = stub_api({("GET", "/runs/r1/eval"): ev, ("GET", "/runs"): []})

    at = open_page("evaluation", api, run_id="r1")

    assert at.error == []
    shown = texts(at)
    assert "注入错误" in shown and "66.7%" in shown
    table = next(d.value for d in at.dataframe if "错误类型" in d.value.columns)
    assert list(table["错误类型"]) == ["OE", "MSRP", "零售"]
    assert list(table["绿"]) == [1, 0, 0]
