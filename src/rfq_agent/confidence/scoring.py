"""Score resolved rows with the configured strategy (spec 12.2)."""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar, get_args

from langchain_core.callbacks import BaseCallbackHandler

from rfq_agent.confidence.evidence import (
    apply_gates,
    band_for,
    evidence_score,
    evidence_signals,
    gates,
    red_cap,
    round_score,
    wants_judge,
    wants_self_check,
)
from rfq_agent.confidence.llm_checks import judge, self_assess, system_msrp, wants_retail
from rfq_agent.config import Settings, SignalName
from rfq_agent.partno import normalize_part_no
from rfq_agent.pricing import msrp_matches
from rfq_agent.schemas import ConfidenceResult, ResolvedRow, StrategyName
from rfq_agent.trace import TraceLLMCallback, TraceWriter

T = TypeVar("T")

TRACE_AGENT = "confidence"


@dataclass
class _Scored:
    """Score parts gathered for one row before it is finalized."""

    raw: int  # evidence score before gates
    evidence: int  # gated evidence score; LLM parts blend with this
    signals: dict[str, float]
    caps: dict[str, int]
    score: int
    notes: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)


def expected_llm_calls(
    rows: Sequence[ResolvedRow], settings: Settings, strategy: StrategyName
) -> int:
    """LLM calls ``score_rows`` will make (before structured-output retries)."""
    total = 0
    for row in rows:
        scored = _evidence_part(row, settings)
        if _runs_self(scored, strategy):
            total += settings.confidence.self_samples
        if _runs_judge(scored, strategy, settings):
            total += 1
    return total


def _evidence_part(row: ResolvedRow, settings: Settings) -> _Scored:
    """Signals -> evidence score -> gates: the deterministic part every strategy starts from."""
    signals = evidence_signals(row, settings)
    evidence = evidence_score(signals, settings)
    caps = gates(row, settings)
    gated = apply_gates(evidence, caps)
    return _Scored(
        raw=evidence,
        evidence=gated,
        signals={**signals, "evidence_score": float(evidence), **_floats(caps)},
        caps=caps,
        score=gated,
    )


def _runs_self(scored: _Scored, strategy: StrategyName) -> bool:
    return strategy == "evidence_plus_self" and wants_self_check(scored.caps)


def _runs_judge(scored: _Scored, strategy: StrategyName, settings: Settings) -> bool:
    return strategy == "evidence_plus_judge" and wants_judge(scored.evidence, settings)


async def score_rows(
    rows: Sequence[ResolvedRow],
    settings: Settings,
    strategy: StrategyName | None = None,
    *,
    run_id: str | None = None,
    trace: TraceWriter | None = None,
) -> list[ResolvedRow]:
    """Return copies of ``rows`` with confidence, score, band and judge concerns filled in."""
    name: StrategyName = strategy or settings.confidence.strategy
    callbacks: list[BaseCallbackHandler] = [TraceLLMCallback(trace)] if trace else []
    return list(
        await asyncio.gather(
            *(_score_row(row, settings, name, run_id, callbacks, trace) for row in rows)
        )
    )


async def _score_row(
    row: ResolvedRow,
    settings: Settings,
    strategy: StrategyName,
    run_id: str | None,
    callbacks: list[BaseCallbackHandler],
    trace: TraceWriter | None,
) -> ResolvedRow:
    scored = _evidence_part(row, settings)
    if _runs_self(scored, strategy):
        await _add_self(scored, row, settings, run_id, callbacks)
    if _runs_judge(scored, strategy, settings):
        await _add_judge(scored, row, settings, run_id, callbacks)
    return _finish(row, scored, strategy, settings, trace)


def _floats(caps: dict[str, int]) -> dict[str, float]:
    return {name: float(cap) for name, cap in caps.items()}


# --- evidence_plus_self -------------------------------------------------------------------


async def _add_self(
    scored: _Scored,
    row: ResolvedRow,
    settings: Settings,
    run_id: str | None,
    callbacks: list[BaseCallbackHandler],
) -> None:
    samples = await self_assess(row, settings, run_id=run_id, callbacks=callbacks)
    if not samples:
        scored.notes.append("LLM self-assessment failed; evidence score used")
        return
    system_oe = normalize_part_no(row.worksheet.current_oe or "")
    judgments = {
        "oe": _judgment(
            [normalize_part_no(s.oe_choice) for s in samples],
            [s.oe_confidence for s in samples],
            system_oe,
            _same,
        )
    }
    if wants_retail(row):
        judgments["retail"] = _judgment(
            [s.retail_choice for s in samples],
            [s.retail_confidence for s in samples],
            row.worksheet.selected_retail_source,
            _same,
        )
    msrp = system_msrp(row)
    if msrp is not None:
        judgments["msrp"] = _judgment(
            [s.msrp_value for s in samples],
            [s.msrp_confidence for s in samples],
            msrp,
            msrp_matches,
        )
    for key, (score, consistency) in judgments.items():
        scored.signals[f"self_{key}_score"] = score
        scored.signals[f"self_{key}_consistency"] = consistency
    llm = statistics.fmean(0.5 * s + 0.5 * c for s, c in judgments.values())
    scored.signals["llm_signal"] = llm
    weight = settings.confidence.self_weight
    llm_score = 100 * llm
    scored.score = round_score((1 - weight) * scored.evidence + weight * llm_score)
    if len(samples) < settings.confidence.self_samples:
        scored.notes.append(f"only {len(samples)} self-assessment sample(s) succeeded")
    scored.notes.append(
        f"LLM self-assessment signal {llm:.2f}: "
        f"{_blend_text(weight, scored.evidence, llm_score)} -> {scored.score}"
    )


def _same(answer: str, system: str) -> bool:
    return answer == system


def _judgment(
    answers: Sequence[T | None],
    confidences: Sequence[int | None],
    system: T | None,
    matches: Callable[[T, T], bool],
) -> tuple[float, float]:
    """(score, consistency) of one key judgment over the samples.

    Score: mean confidence of the samples whose answer ``matches`` the system's. Consistent:
    every sample left it empty, or every answer matches the first one.
    """
    score = statistics.fmean(
        (conf or 0) / 100
        if answer is not None and system is not None and matches(answer, system)
        else 0.0
        for answer, conf in zip(answers, confidences, strict=True)
    )
    given = [a for a in answers if a is not None]
    if not given:
        consistent = True
    else:
        consistent = len(given) == len(answers) and all(matches(a, given[0]) for a in given)
    return score, 1.0 if consistent else 0.0


# --- evidence_plus_judge ------------------------------------------------------------------


async def _add_judge(
    scored: _Scored,
    row: ResolvedRow,
    settings: Settings,
    run_id: str | None,
    callbacks: list[BaseCallbackHandler],
) -> None:
    verdict = await judge(row, settings, run_id=run_id, callbacks=callbacks)
    if verdict is None:
        scored.notes.append("judge review failed; evidence score used")
        return
    scored.signals["judge_agree"] = 1.0 if verdict.agree else 0.0
    scored.signals["judge_score"] = float(verdict.score)
    if verdict.agree:
        # The judge can only lower a score: agreeing keeps the gated evidence score, so
        # rows it failed to see through are not lifted into green (spec 12.2, P10).
        scored.notes.append(f"judge agreed (score {verdict.score}): evidence score kept")
        if verdict.concerns:
            scored.review.append(f"judge note: {verdict.concerns}")
        return
    weight = settings.confidence.judge_weight
    blended = round_score((1 - weight) * scored.evidence + weight * verdict.score)
    blend = f"{_blend_text(weight, scored.evidence, verdict.score)} -> {blended}"
    # Never above the red cap, and never above the evidence score: the judge only lowers.
    cap = red_cap(settings)
    scored.score = min(blended, cap, scored.evidence)
    label = "red" if scored.score == cap else "evidence score"
    held = f", held at {scored.score} ({label})" if blended > scored.score else ""
    scored.notes.append(f"judge disagreed (score {verdict.score}): {blend}{held}")
    scored.review.append(f"judge: {verdict.concerns or 'disagrees with the conclusions'}")


# --- finish -------------------------------------------------------------------------------


def _finish(
    row: ResolvedRow,
    scored: _Scored,
    strategy: StrategyName,
    settings: Settings,
    trace: TraceWriter | None,
) -> ResolvedRow:
    final = apply_gates(scored.score, scored.caps)
    band = band_for(final, settings)
    confidence = ConfidenceResult(
        row_id=row.line.row_id,
        score=final,
        band=band,
        strategy=strategy,
        signals=scored.signals,
        explanation=_explanation(scored, final),
    )
    worksheet = row.worksheet.model_copy(
        update={
            "confidence_score": final,
            "band": band,
            "review_reasons": [*row.worksheet.review_reasons, *scored.review],
        }
    )
    if trace is not None:
        trace.event(row.line.row_id, TRACE_AGENT, "score", confidence=confidence)
    return row.model_copy(update={"worksheet": worksheet, "confidence": confidence})


def _blend_text(weight: float, gated: int, llm_score: float) -> str:
    """``0.7 x 79 + 0.3 x 90``: the gated evidence score and the LLM score being blended."""
    return f"{1 - weight:g} x {gated} + {weight:g} x {llm_score:g}"


def _explanation(scored: _Scored, final: int) -> str:
    """Evidence score and its gate, then the LLM blend, then a gate that caps the blend."""
    base = {k: v for k, v in scored.signals.items() if k in get_args(SignalName)}
    weakest = ", ".join(f"{k} {v:.2f}" for k, v in sorted(base.items(), key=lambda kv: kv[1])[:2])
    binding: list[str] = []
    head = f"evidence score {scored.raw}"
    if scored.evidence < scored.raw:
        binding += _gates_at(scored.caps, scored.evidence)
        head += f", capped at {scored.evidence} by {', '.join(binding)}"
    parts = [f"{head} (weakest signals: {weakest})", *scored.notes]
    if final < scored.score:
        final_gates = _gates_at(scored.caps, final)
        binding += final_gates
        parts.append(f"final capped at {final} by {', '.join(final_gates)}")
    idle = [name for name in scored.caps if name not in binding]
    if idle:
        parts.append("gates not binding: " + ", ".join(idle))
    return "; ".join(parts)


def _gates_at(caps: dict[str, int], cap: int) -> list[str]:
    return [name for name, value in caps.items() if value == cap]
