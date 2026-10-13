"""The three confidence strategies (spec 12.2) through ``score_rows``, in fake LLM mode."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from rfq_agent.confidence import fake_handlers as confidence_fakes
from rfq_agent.confidence import score_rows
from rfq_agent.config import Settings
from rfq_agent.llm.fake import FakeRequest, register_fake_handler
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import OECandidate, ResolvedRow, RowFailure, StrategyName
from tests.builders import cost, line, msrp, oe, retail

SELF, JUDGE = "SelfAssessment", "JudgeVerdict"


def clean(row_id: int) -> tuple[Any, ...]:
    """Evidence score 92: interchange OE, MSRP 100, retail 80/82/85, specs agree."""
    return line(row_id), oe(row_id), msrp(row_id), retail(row_id), cost(row_id)


def insufficient(row_id: int) -> tuple[Any, ...]:
    """Evidence score 72 (spec signal 0), capped at 79 by insufficient data."""
    data = cost(row_id, status="insufficient_data", spec_source="missing", breakdown=None)
    return line(row_id), oe(row_id, current=f"P-{row_id}"), msrp(row_id), retail(row_id), data


def multiple(row_id: int) -> tuple[Any, ...]:
    cands = [
        OECandidate(oe_part_no=pn, brand=None, match_type="interchange", evidence=[])
        for pn in (f"A-{row_id}", f"B-{row_id}")
    ]
    multi = oe(row_id, status="multiple", current_oe=None, supersession_chain=[], candidates=cands)
    return line(row_id), multi, msrp(row_id, None), retail(row_id, {}), cost(row_id)


def rows_of(*parts: tuple[Any, ...]) -> list[ResolvedRow]:
    lines, oes, msrps, retails, costs = (list(x) for x in zip(*parts, strict=True))
    return resolve_rows(lines, oes, msrps, retails, costs)


@pytest.fixture
def calls() -> Iterator[dict[str, int]]:
    """Count fake LLM calls per schema while keeping the default fake answers."""
    counts = {SELF: 0, JUDGE: 0}

    def counting(name: str, fn: Callable[[FakeRequest], dict[str, Any]]) -> None:
        def handler(request: FakeRequest) -> dict[str, Any]:
            counts[name] += 1
            return fn(request)

        register_fake_handler(name)(handler)

    counting(SELF, confidence_fakes.fake_self_assessment)
    counting(JUDGE, confidence_fakes.fake_judge)
    yield counts
    register_fake_handler(SELF)(confidence_fakes.fake_self_assessment)
    register_fake_handler(JUDGE)(confidence_fakes.fake_judge)


def answer(name: str, fn: Callable[[FakeRequest], dict[str, Any]]) -> None:
    register_fake_handler(name)(fn)


# --- evidence ----------------------------------------------------------------------------


async def test_evidence_strategy_scores_without_llm(
    settings: Settings, calls: dict[str, int]
) -> None:
    [row] = await score_rows(rows_of(clean(1)), settings, strategy="evidence", run_id="t")

    assert row.confidence is not None
    assert row.confidence.score == 92 and row.confidence.band == "green"
    assert row.confidence.strategy == "evidence"
    assert row.confidence.signals["evidence_score"] == 92
    assert row.worksheet.confidence_score == 92 and row.worksheet.band == "green"
    assert "evidence score 92" in row.confidence.explanation
    assert calls == {SELF: 0, JUDGE: 0}


async def test_gated_row_is_red_and_says_why(settings: Settings) -> None:
    [row] = await score_rows(rows_of(multiple(1)), settings, strategy="evidence", run_id="t")

    assert row.confidence is not None
    assert row.confidence.score == 49 and row.confidence.band == "red"
    assert row.confidence.signals["gate_oe_multiple"] == 49
    assert "capped at 49 by gate_oe_multiple" in row.confidence.explanation


async def test_strategy_defaults_to_settings(settings: Settings) -> None:
    settings.confidence.strategy = "evidence_plus_judge"
    [row] = await score_rows(rows_of(clean(1)), settings, run_id="t")
    assert row.confidence is not None and row.confidence.strategy == "evidence_plus_judge"


# --- evidence_plus_self ------------------------------------------------------------------


async def test_self_samples_twice_per_row_except_red_gated(
    settings: Settings, calls: dict[str, int]
) -> None:
    rows = await score_rows(
        rows_of(clean(1), multiple(2)), settings, strategy="evidence_plus_self", run_id="t"
    )
    agreeing, gated = (r.confidence for r in rows)
    assert agreeing is not None and gated is not None
    # Fake answers match the system at confidence 80 in both samples:
    # each judgment 0.5 * 0.8 + 0.5 * 1 = 0.9; final 0.7 * 92 + 0.3 * 90 = 91.4
    assert agreeing.signals["llm_signal"] == pytest.approx(0.9)
    assert agreeing.score == 91
    assert gated.score == 49 and "llm_signal" not in gated.signals
    assert calls[SELF] == 2


async def test_self_disagreement_and_inconsistency_lower_the_score(settings: Settings) -> None:
    oe_answers = iter([("NEW-3", 90), ("OTHER-1", 60)])

    def handler(request: FakeRequest) -> dict[str, Any]:
        oe_choice, oe_conf = next(oe_answers)
        return {
            "oe_choice": oe_choice,
            "oe_confidence": oe_conf,
            "retail_choice": "retail_2",
            "retail_confidence": 70,
            "msrp_value": 100.0,
            "msrp_confidence": 70,
            "reasoning": "scripted",
        }

    answer(SELF, handler)
    try:
        [row] = await score_rows(
            rows_of(clean(1)), settings, strategy="evidence_plus_self", run_id="t"
        )
    finally:
        answer(SELF, confidence_fakes.fake_self_assessment)

    signals = row.confidence.signals if row.confidence else {}
    # OE: score (0.9 + 0) / 2 = 0.45, answers differ -> 0.5 * 0.45 + 0 = 0.225
    # retail: both pick retail_2, the system picked retail_1 -> 0 + 0.5 * 1 = 0.5
    assert signals["self_oe_score"] == pytest.approx(0.45)
    assert signals["self_oe_consistency"] == 0
    assert signals["self_retail_score"] == 0
    assert signals["self_retail_consistency"] == 1
    # MSRP: both read 100.0 (the system's), confidence 70 -> 0.5 * 0.7 + 0.5 * 1 = 0.85
    assert signals["self_msrp_score"] == pytest.approx(0.7)
    assert signals["self_msrp_consistency"] == 1
    assert signals["llm_signal"] == pytest.approx((0.225 + 0.5 + 0.85) / 3)
    # 0.7 * 92 + 0.3 * 52.5 = 80.15
    assert row.confidence is not None and row.confidence.score == 80


async def test_self_without_retail_reference_judges_oe_and_msrp(settings: Settings) -> None:
    no_quotes = (line(1), oe(1), msrp(1), retail(1, {}), cost(1))
    [row] = await score_rows(rows_of(no_quotes), settings, strategy="evidence_plus_self")
    assert row.confidence is not None
    assert "self_retail_score" not in row.confidence.signals
    assert row.confidence.signals["llm_signal"] == pytest.approx(0.9)


async def test_self_failure_falls_back_to_evidence(settings: Settings) -> None:
    def broken(request: FakeRequest) -> dict[str, Any]:
        raise RuntimeError("model down")

    answer(SELF, broken)
    try:
        [row] = await score_rows(rows_of(clean(1)), settings, strategy="evidence_plus_self")
    finally:
        answer(SELF, confidence_fakes.fake_self_assessment)

    assert row.confidence is not None and row.confidence.score == 92
    assert "LLM self-assessment failed" in row.confidence.explanation
    assert row.worksheet.failures == []


def msrp_reads(*values: float | None) -> Callable[[FakeRequest], dict[str, Any]]:
    """The default fake answers, with the MSRP each sample reads given in turn."""
    reads = iter(values)

    def handler(request: FakeRequest) -> dict[str, Any]:
        value = next(reads)
        return {
            **confidence_fakes.fake_self_assessment(request),
            "msrp_value": value,
            "msrp_confidence": 80 if value is not None else None,
        }

    return handler


@pytest.mark.parametrize(
    ("reads", "score", "consistency"),
    [
        ((100.5, 100.0), 0.8, 1),  # within 1% of the system's 100 and of each other
        ((120.0, 120.0), 0.0, 1),  # agree with each other, not with the system
        ((100.0, 110.0), 0.4, 0),  # one matches; the two differ by 10%
        ((None, None), 0.0, 1),  # no answer counts as not matching
    ],
)
async def test_self_msrp_answers_are_compared_within_one_percent(
    settings: Settings, reads: tuple[float | None, float | None], score: float, consistency: int
) -> None:
    answer(SELF, msrp_reads(*reads))
    try:
        [row] = await score_rows(rows_of(clean(1)), settings, strategy="evidence_plus_self")
    finally:
        answer(SELF, confidence_fakes.fake_self_assessment)
    assert row.confidence is not None
    assert row.confidence.signals["self_msrp_score"] == pytest.approx(score)
    assert row.confidence.signals["self_msrp_consistency"] == consistency


async def test_self_asks_about_msrp_only_with_an_msrp_quote(settings: Settings) -> None:
    prompts: list[str] = []

    def capture(request: FakeRequest) -> dict[str, Any]:
        prompts.append(request.text)
        return confidence_fakes.fake_self_assessment(request)

    no_msrp = (line(2), oe(2, "OWN-2"), msrp(2, None), retail(2), cost(2))  # not shared
    answer(SELF, capture)
    try:
        with_msrp, without = await score_rows(
            rows_of(clean(1), no_msrp), settings, strategy="evidence_plus_self"
        )
    finally:
        answer(SELF, confidence_fakes.fake_self_assessment)
    assert with_msrp.confidence is not None and without.confidence is not None
    assert "self_msrp_score" in with_msrp.confidence.signals
    assert "self_msrp_score" not in without.confidence.signals
    asked = [p for p in prompts if "question 3" in p]
    assert len(asked) == 2  # both samples of row 1
    # spec 11.2 rules the reading follows
    assert all("higher" in p and "USD" in p and "superseded" in p for p in asked)


# --- evidence_plus_judge -----------------------------------------------------------------


async def test_judge_reviews_every_row_without_a_red_gate_and_agreeing_adds_nothing(
    settings: Settings, calls: dict[str, int]
) -> None:
    rows = await score_rows(
        rows_of(clean(1), insufficient(2), multiple(3)),
        settings,
        strategy="evidence_plus_judge",
        run_id="t",
    )
    high, middle, low = (r.confidence for r in rows)
    assert high is not None and middle is not None and low is not None
    assert calls[JUDGE] == 2
    # fake judge agrees with score 70: the evidence score is kept, green rows included
    assert high.signals["judge_agree"] == 1 and high.score == 92 and high.band == "green"
    assert middle.signals["judge_score"] == 70 and middle.score == 72
    assert "judge agreed (score 70): evidence score kept" in middle.explanation
    assert low.score == 49 and "judge_score" not in low.signals


async def test_judge_disagreement_sends_the_row_to_red(settings: Settings) -> None:
    answer(JUDGE, lambda r: {"agree": False, "score": 85, "concerns": "MSRP looks stale"})
    try:
        [row] = await score_rows(rows_of(insufficient(1)), settings, strategy="evidence_plus_judge")
    finally:
        answer(JUDGE, confidence_fakes.fake_judge)

    assert row.confidence is not None
    assert row.confidence.score == 49 and row.confidence.band == "red"
    assert "judge: MSRP looks stale" in row.worksheet.review_reasons
    # 0.5 * 72 + 0.5 * 85 = 78.5 -> 79, above the red cap
    assert "-> 79, held at 49 (red)" in row.confidence.explanation


async def test_low_judge_blend_is_not_described_as_held(settings: Settings) -> None:
    answer(JUDGE, lambda r: {"agree": False, "score": 20, "concerns": "wrong part"})
    try:
        [row] = await score_rows(rows_of(insufficient(1)), settings, strategy="evidence_plus_judge")
    finally:
        answer(JUDGE, confidence_fakes.fake_judge)

    assert row.confidence is not None
    # 0.5 * 72 + 0.5 * 20 = 46, already below the red cap
    assert row.confidence.score == 46
    assert "judge disagreed (score 20): 0.5 x 72 + 0.5 x 20 -> 46" in row.confidence.explanation
    assert "held at" not in row.confidence.explanation


async def test_judge_prompt_states_the_business_rules_as_intended(settings: Settings) -> None:
    seen: list[str] = []

    def capture(request: FakeRequest) -> dict[str, Any]:
        seen.append(request.text.lower())
        return confidence_fakes.fake_judge(request)

    answer(JUDGE, capture)
    try:
        await score_rows(rows_of(insufficient(1)), settings, strategy="evidence_plus_judge")
    finally:
        answer(JUDGE, confidence_fakes.fake_judge)

    [prompt] = seen
    # spec 12.2: the rules that shape how the evidence reads, stated as intended behavior
    for rule in (
        "internal database wins",  # 11.4 spec conflict
        "higher price",  # 11.2 conflicting MSRP prices
        "superseded",  # 11.2 fallback along the chain
        "usd",  # 11.2 / 11.3 currency
        "40% from the median",  # 11.3 outlier threshold, from retail_outlier_threshold 0.40
        "match quality",  # 11.3 selection order
        "multiple",  # 11.2 / 11.3 no price lookup when OE is multiple
        "not concerns",
    ):
        assert rule in prompt, rule


async def test_judge_concerns_on_agreement_become_a_review_note(settings: Settings) -> None:
    answer(JUDGE, lambda r: {"agree": True, "score": 80, "concerns": "MSRP above retail"})
    try:
        [row] = await score_rows(rows_of(insufficient(1)), settings, strategy="evidence_plus_judge")
    finally:
        answer(JUDGE, confidence_fakes.fake_judge)

    assert row.confidence is not None and row.confidence.score == 72  # evidence score kept
    assert "judge note: MSRP above retail" in row.worksheet.review_reasons


async def test_judge_agreement_without_concerns_adds_no_review_reason(settings: Settings) -> None:
    [before] = rows_of(insufficient(1))
    [row] = await score_rows([before], settings, strategy="evidence_plus_judge")
    assert row.worksheet.review_reasons == before.worksheet.review_reasons


async def test_judge_failure_falls_back_to_evidence(settings: Settings) -> None:
    def broken(request: FakeRequest) -> dict[str, Any]:
        raise RuntimeError("model down")

    answer(JUDGE, broken)
    try:
        [row] = await score_rows(rows_of(insufficient(1)), settings, strategy="evidence_plus_judge")
    finally:
        answer(JUDGE, confidence_fakes.fake_judge)

    assert row.confidence is not None and row.confidence.score == 72
    assert "judge review failed" in row.confidence.explanation


# --- review fixes ------------------------------------------------------------------------


def weak_but_ungated(row_id: int) -> tuple[Any, ...]:
    """Evidence score 39 with no gate: truncated chain, no MSRP, no quotes, spec conflict.

    oe 0.7*0.25 + chain 0.3*0.10 + sources 0 + dispersion 0.5*0.15 + ratio 0.5*0.10
    + spec 0.3*0.20 = 0.39
    """
    return (
        line(row_id),
        oe(row_id, current=f"W-{row_id}", chain_truncated=True),
        msrp(row_id, None),
        retail(row_id, {}),
        cost(row_id, spec_source="conflict"),
    )


def shared_disagreeing() -> tuple[tuple[Any, ...], ...]:
    """Two rows sharing NEW-3 with different MSRPs: evidence 92 each, capped at 79."""
    return (
        (line(1), oe(1), msrp(1, 100.0), retail(1), cost(1)),
        (line(2), oe(2), msrp(2, 120.0), retail(2), cost(2)),
    )


async def test_self_also_runs_on_low_rows_without_a_red_gate(
    settings: Settings, calls: dict[str, int]
) -> None:
    [row] = await score_rows(rows_of(weak_but_ungated(1)), settings, strategy="evidence_plus_self")
    assert row.confidence is not None
    assert row.confidence.signals["evidence_score"] == 39
    assert calls[SELF] == 2
    # OE judgment only (no retail reference): 0.9; 0.7 * 39 + 0.3 * 90 = 54.3
    assert row.confidence.score == 54


async def test_judge_disagreement_blends_with_the_gated_evidence_score(
    settings: Settings,
) -> None:
    answer(JUDGE, lambda r: {"agree": False, "score": 10, "concerns": "wrong OE"})
    try:
        rows = await score_rows(
            rows_of(*shared_disagreeing()), settings, strategy="evidence_plus_judge"
        )
    finally:
        answer(JUDGE, confidence_fakes.fake_judge)
    for row in rows:
        assert row.confidence is not None
        # gated 79, judge 10: 0.5 * 79 + 0.5 * 10 = 44.5 -> 45 (not 0.5 * 92 + 5 = 51)
        assert row.confidence.score == 45


async def test_self_blends_with_the_gated_evidence_score(settings: Settings) -> None:
    def lukewarm(request: FakeRequest) -> dict[str, Any]:
        # OE right but with confidence 0; retail consistent but wrong -> each judgment 0.5
        return {
            "oe_choice": "NEW-3",
            "oe_confidence": 0,
            "retail_choice": "retail_9",
            "retail_confidence": 90,
            "reasoning": "scripted",
        }

    answer(SELF, lukewarm)
    try:
        rows = await score_rows(
            rows_of(*shared_disagreeing()), settings, strategy="evidence_plus_self"
        )
    finally:
        answer(SELF, confidence_fakes.fake_self_assessment)

    for row in rows:
        assert row.confidence is not None
        assert row.confidence.signals["llm_signal"] == pytest.approx(0.5)
        # gated 79: 0.7 * 79 + 0.3 * 50 = 70.3 (from the raw 92 it would be 79.4)
        assert row.confidence.score == 70


async def test_self_cannot_lift_a_row_above_its_gate(settings: Settings) -> None:
    settings.confidence.self_weight = 1.0  # final score is the LLM part alone
    [row] = await score_rows(rows_of(insufficient(1)), settings, strategy="evidence_plus_self")
    # llm 0.9 -> 90, capped at 79 by insufficient data
    assert row.confidence is not None and row.confidence.score == 79


async def test_explanation_shows_the_gate_before_the_judge_blend(settings: Settings) -> None:
    rows = await score_rows(
        rows_of(*shared_disagreeing()), settings, strategy="evidence_plus_judge"
    )
    assert rows[0].confidence is not None
    text = rows[0].confidence.explanation
    assert text.startswith("evidence score 92, capped at 79 by gate_shared_disagree")
    assert "judge agreed (score 70): evidence score kept" in text
    assert "final capped" not in text


async def test_explanation_flags_a_gate_that_caps_the_blend_again(settings: Settings) -> None:
    rows = await score_rows(rows_of(*shared_disagreeing()), settings, strategy="evidence_plus_self")
    assert rows[0].confidence is not None
    text = rows[0].confidence.explanation
    # 0.7 * 79 + 0.3 * 90 = 82.3 -> 82, then held at 79
    assert "0.7 x 79 + 0.3 x 90 -> 82" in text
    assert text.endswith("final capped at 79 by gate_shared_disagree")


@pytest.mark.parametrize(
    ("band", "judged"), [((92, 95), True), ((93, 95), False), ((80, 92), True), ((80, 91), False)]
)
async def test_judge_band_bounds_are_inclusive(
    settings: Settings, calls: dict[str, int], band: tuple[int, int], judged: bool
) -> None:
    settings.confidence.judge_band = band
    await score_rows(rows_of(clean(1)), settings, strategy="evidence_plus_judge")
    assert calls[JUDGE] == int(judged)


def diverging_oe(fail_first: bool) -> Callable[[FakeRequest], dict[str, Any]]:
    """Two samples answering different OE numbers; optionally the first one fails."""
    answers = iter(["NEW-3", "OTHER-1"])
    failures = iter([fail_first, False])

    def handler(request: FakeRequest) -> dict[str, Any]:
        if next(failures):
            raise RuntimeError("timeout")
        return {"oe_choice": next(answers), "oe_confidence": 80, "reasoning": "scripted"}

    return handler


@pytest.mark.parametrize(("fail_first", "consistency"), [(False, 0), (True, 1)])
async def test_one_failed_self_sample_keeps_the_other(
    settings: Settings, fail_first: bool, consistency: int
) -> None:
    no_quotes = (line(1), oe(1), msrp(1), retail(1, {}), cost(1))
    answer(SELF, diverging_oe(fail_first))
    try:
        [row] = await score_rows(rows_of(no_quotes), settings, strategy="evidence_plus_self")
    finally:
        answer(SELF, confidence_fakes.fake_self_assessment)

    assert row.confidence is not None
    # Both samples succeed: NEW-3 vs OTHER-1 disagree. One fails: a single answer is consistent.
    assert row.confidence.signals["self_oe_consistency"] == consistency
    note = "only 1 self-assessment sample(s) succeeded"
    assert (note in row.confidence.explanation) is fail_first


@pytest.mark.parametrize("strategy", ["evidence", "evidence_plus_self", "evidence_plus_judge"])
async def test_expected_calls_match_the_calls_made(
    settings: Settings, calls: dict[str, int], strategy: StrategyName
) -> None:
    from rfq_agent.confidence import expected_llm_calls

    rows = rows_of(clean(1), insufficient(2), multiple(3), weak_but_ungated(4))
    expected = expected_llm_calls(rows, settings, strategy)
    await score_rows(rows, settings, strategy=strategy)
    # self: rows 1, 2, 4 (no red gate) x 2 samples; judge: rows 1 (92) and 2 (72), score >= 50
    assert expected == {"evidence": 0, "evidence_plus_self": 6, "evidence_plus_judge": 2}[strategy]
    assert expected == calls[SELF] + calls[JUDGE]


@pytest.mark.parametrize("strategy", ["evidence", "evidence_plus_self", "evidence_plus_judge"])
async def test_explanation_lists_gates_that_never_bind(
    settings: Settings, strategy: StrategyName
) -> None:
    failed = RowFailure(row_id=1, stage="search_chain", error="timeout")
    short = cost(1, status="insufficient_data", spec_source="missing", breakdown=None)
    row_parts = (line(1), oe(1), failed, retail(1), short)
    [row] = await score_rows(rows_of(row_parts), settings, strategy=strategy)

    assert row.confidence is not None
    text = row.confidence.explanation
    assert "capped at 49 by gate_failure" in text
    assert text.endswith("gates not binding: gate_insufficient_data")


async def test_judge_disagreement_never_raises_a_low_score(settings: Settings) -> None:
    settings.confidence.judge_band = (30, 100)  # also reviews rows below the red cap
    answer(JUDGE, lambda r: {"agree": False, "score": 90, "concerns": "unsure"})
    try:
        [row] = await score_rows(
            rows_of(weak_but_ungated(1)), settings, strategy="evidence_plus_judge"
        )
    finally:
        answer(JUDGE, confidence_fakes.fake_judge)
    assert row.confidence is not None
    # 0.5 * 39 + 0.5 * 90 = 64.5; the red cap 49 would lift it, the judge only lowers
    assert row.confidence.score == 39
