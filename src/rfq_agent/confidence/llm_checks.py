"""LLM checks used by the confidence strategies: self-assessment samples and the judge."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TypeVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from rfq_agent.confidence import fake_handlers  # noqa: F401 - registers the fake answers
from rfq_agent.confidence.brief import (
    MSRP_QUESTION,
    OE_QUESTION,
    RETAIL_QUESTION,
    judge_brief,
    self_brief,
)
from rfq_agent.config import Settings
from rfq_agent.llm.structured import StructuredResult, invoke_structured
from rfq_agent.schemas import MSRPResult, ResolvedRow
from rfq_agent.trace import META_ROW_ID

T = TypeVar("T", bound=BaseModel)

SELF_ROLE = "self_eval"
JUDGE_ROLE = "judge"


class SelfAssessment(BaseModel):
    """The model's own answer to the key judgments, with its confidence in each."""

    oe_choice: str = Field(description="The OE number you judge to be the correct current OE.")
    oe_confidence: int = Field(ge=0, le=100, description="Your confidence in oe_choice.")
    retail_choice: str | None = Field(
        default=None, description="Site whose quote is the best pricing reference, if asked."
    )
    retail_confidence: int | None = Field(default=None, ge=0, le=100)
    msrp_value: float | None = Field(
        default=None, description="The MSRP in USD you read from the MSRP pages, if asked."
    )
    msrp_confidence: int | None = Field(default=None, ge=0, le=100)
    reasoning: str = Field(description="One or two sentences.")


class JudgeVerdict(BaseModel):
    """An independent reviewer's verdict on the row's conclusions."""

    agree: bool = Field(description="Whether the system's conclusions are supported.")
    score: int = Field(ge=0, le=100, description="Confidence the row can be used as is.")
    concerns: str = Field(description="Main concern if any, otherwise an empty string.")


SELF_SYSTEM = """You check an automotive aftermarket RFQ line. From the evidence below, answer
independently:
1. Which OE number is the correct *current* OE for this customer part? Follow supersession
   (old -> new) to the newest number. Give your confidence (0-100).
2. Only if asked: which retail site's quote is the most reliable pricing reference? Rules:
   never pick an outlier; prefer the best match quality (exact > normalized > fuzzy);
   prefer in-stock quotes; then the lowest effective price. Give your confidence (0-100).
3. Only if asked: from the MSRP pages, what MSRP (USD) does the manufacturer list for this
   part? Rules: only USD prices count (a page without a currency counts as USD); if one
   number has several prices, take the higher; if the current OE has no price, the price of a
   superseded (older) number in the chain may be used. Give your confidence (0-100).
Use only the evidence given."""

JUDGE_SYSTEM = """You are an independent reviewer of an automotive aftermarket RFQ pricing line.
You see the evidence collected by the system and its conclusions (OE number, MSRP, selected
retail price, should-cost spec). Decide whether the conclusions are supported by the evidence.
Return agree (true/false), a score 0-100 for how confident you are that the line can be used
without human changes, and the main concern (empty if none). Use only the evidence given.

{rules}"""

# The business rules the pipeline applies on purpose, so the judge does not flag them as
# problems (spec 12.2, decided after P9). The only copy of this text; keep it in step with
# the spec sections named on each line.
JUDGE_RULES = """The system applies these business rules on purpose. They are not concerns;
only check that the evidence was read correctly and that the rules were applied correctly:
- Spec (11.4): when the internal database and an external page disagree on material or
  weight, the internal database wins; external pages only fill fields the internal record
  lacks.
- MSRP (11.2): when one number has several conflicting prices, the higher price is taken.
  When the current OE has no price, the price of a superseded (older) number in the chain
  may be used. Only USD prices are used; pages without a currency count as USD.
- Retail (11.3): a quote is an outlier when its effective price is more than
  {outlier_pct} from the median of the row's quotes. The reference quote is chosen among
  non-outliers by match quality (exact > normalized > fuzzy), then in stock, then lowest
  effective price.
- OE (11.2, 11.3): when the OE is multiple, no MSRP or retail price is looked up."""


def judge_system(settings: Settings) -> str:
    """The judge's system prompt with the business rules filled in from the settings."""
    threshold = settings.pipeline.retail_outlier_threshold
    rules = JUDGE_RULES.format(outlier_pct=f"{threshold:.0%}")
    return JUDGE_SYSTEM.format(rules=rules)


def wants_retail(row: ResolvedRow) -> bool:
    return row.worksheet.selected_retail_source is not None


def system_msrp(row: ResolvedRow) -> float | None:
    """The MSRP the system took for the row (its own result, not a manual price)."""
    msrp = row.msrp
    if isinstance(msrp, MSRPResult) and msrp.quote is not None:
        return msrp.quote.effective_price
    return None


async def self_assess(
    row: ResolvedRow,
    settings: Settings,
    *,
    run_id: str | None,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> list[SelfAssessment]:
    """``self_samples`` independent samples; failed samples are dropped."""
    asked = [OE_QUESTION]
    if wants_retail(row):
        asked.append(RETAIL_QUESTION)
    if system_msrp(row) is not None:
        asked.append(MSRP_QUESTION)
    question = f"Answer {' and '.join(asked)}; leave the fields of any other question empty."
    messages = [
        SystemMessage(content=SELF_SYSTEM),
        HumanMessage(content=f"{self_brief(row)}\n\n{question}"),
    ]
    results = await asyncio.gather(
        *(
            _invoke(SelfAssessment, messages, SELF_ROLE, row, settings, run_id, callbacks)
            for _ in range(settings.confidence.self_samples)
        )
    )
    return [r.value for r in results if r.ok and r.value is not None]


async def judge(
    row: ResolvedRow,
    settings: Settings,
    *,
    run_id: str | None,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> JudgeVerdict | None:
    """The judge's verdict, or None when the call failed."""
    messages = [
        SystemMessage(content=judge_system(settings)),
        HumanMessage(content=judge_brief(row)),
    ]
    result = await _invoke(JudgeVerdict, messages, JUDGE_ROLE, row, settings, run_id, callbacks)
    return result.value if result.ok else None


async def _invoke(
    schema: type[T],
    messages: Sequence[BaseMessage],
    role: str,
    row: ResolvedRow,
    settings: Settings,
    run_id: str | None,
    callbacks: Sequence[BaseCallbackHandler],
) -> StructuredResult[T]:
    return await invoke_structured(
        schema,
        messages,
        role=role,
        run_id=run_id,
        agent=f"confidence_{role}",
        settings=settings,
        metadata={META_ROW_ID: row.line.row_id},
        callbacks=callbacks,
    )
