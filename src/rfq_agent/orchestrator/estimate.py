"""Expected LLM calls of a run, per role (spec 13 cost guard; CLI and API)."""

from __future__ import annotations

from typing import NamedTuple

from rfq_agent.agents.oe_interchange import MAX_HOPS
from rfq_agent.confidence.llm_checks import JUDGE_ROLE, SELF_ROLE
from rfq_agent.confidence.scoring import expected_llm_calls
from rfq_agent.config import Settings
from rfq_agent.schemas import ResolvedRow, StrategyName


class CallEstimate(NamedTuple):
    expected: int
    upper: int


Calls = dict[str, CallEstimate]

# The LLM role each confidence strategy adds; strategies not listed call no LLM.
CONFIDENCE_ROLES: dict[StrategyName, str] = {
    "evidence_plus_self": SELF_ROLE,
    "evidence_plus_judge": JUDGE_ROLE,
}

DOWNSTREAM_AGENTS = ("oe_msrp", "retail_price", "should_cost")


def run_calls(settings: Settings, strategy: StrategyName, rows: int) -> Calls:
    """Per-role calls of running ``rows`` rows; price agents assume no shared OE."""
    retries = _attempts(settings)
    calls = {"oe_interchange": CallEstimate(rows * 3, rows * (1 + MAX_HOPS) * retries)}
    calls |= {agent: CallEstimate(rows, rows * retries) for agent in DOWNSTREAM_AGENTS}
    return calls | _confidence_calls(settings, strategy, rows)


def review_calls(settings: Settings, strategy: StrategyName) -> Calls:
    """A new OE number in review: the downstream agents and rescoring, for that one row."""
    retries = _attempts(settings)
    calls = {agent: CallEstimate(1, retries) for agent in DOWNSTREAM_AGENTS}
    return calls | _confidence_calls(settings, strategy, 1)


def rescore_calls(settings: Settings, strategy: StrategyName, rows: list[ResolvedRow]) -> Calls:
    """Confidence calls of re-scoring finalized ``rows``: exact, their evidence scores are known."""
    role = CONFIDENCE_ROLES.get(strategy)
    if role is None:
        return {}
    expected = expected_llm_calls(rows, settings, strategy)
    return {role: CallEstimate(expected, expected * _attempts(settings))}


def real_calls(settings: Settings, calls: Calls) -> Calls:
    """The part of ``calls`` whose roles use a real (paid) provider."""
    return {role: n for role, n in calls.items() if settings.llm.for_role(role).provider != "fake"}


def _confidence_calls(settings: Settings, strategy: StrategyName, rows: int) -> Calls:
    role = CONFIDENCE_ROLES.get(strategy)
    if role is None:
        return {}
    per_row = settings.confidence.self_samples if role == SELF_ROLE else 1
    return {role: CallEstimate(rows * per_row, rows * per_row * _attempts(settings))}


def _attempts(settings: Settings) -> int:
    return 1 + settings.pipeline.structured_output_retries
