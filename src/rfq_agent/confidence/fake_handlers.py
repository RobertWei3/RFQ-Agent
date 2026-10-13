"""FakeLLM answers for the confidence checks.

The self-assessment fake derives its answers from the evidence in the brief the way the
business rules describe (chain tail; best non-outlier retail quote) at confidence 80; the
brief itself carries no system conclusion. The judge fake
always agrees with score 70. Neither reads ground truth.
"""

from __future__ import annotations

import re
from typing import Any

from rfq_agent.confidence.brief import (
    CHAIN_PREFIX,
    MSRP_HEADER,
    MSRP_QUESTION,
    RETAIL_HEADER,
    RETAIL_QUESTION,
)
from rfq_agent.llm.fake import FakeRequest, register_fake_handler

FAKE_SELF_CONFIDENCE = 80
FAKE_JUDGE_SCORE = 70

_CANDIDATE = re.compile(r"^- (\S+) \(brand ", re.M)
_QUOTE = re.compile(
    r"^- (\S+): .*?effective (\S+) .*?in stock (\w+), match quality (\w+), outlier (\w+)$",
    re.M,
)
_MATCH_RANK = {"exact": 0, "normalized": 1, "fuzzy": 2}
# A money amount in an MSRP snippet: "$74.76" or "USD 61.89" (not the digits of a part number).
_AMOUNT = re.compile(r"(?:\$|USD)\s?(\d+(?:\.\d+)?)")


def _oe_choice(text: str) -> str:
    for line in text.splitlines():
        if line.startswith(CHAIN_PREFIX):
            return line.removeprefix(CHAIN_PREFIX).split(" -> ")[-1]
    found = _CANDIDATE.search(text)
    return found.group(1) if found else ""


def _retail_choice(text: str) -> str | None:
    if RETAIL_HEADER not in text or RETAIL_QUESTION not in text:
        return None
    usable = [
        (site, float(price), stock == "yes", match)
        for site, price, stock, match, outlier in _QUOTE.findall(text)
        if outlier == "no" and price != "-"
    ]
    if not usable:
        return None
    best = min(usable, key=lambda q: (_MATCH_RANK.get(q[3], 3), not q[2], q[1], q[0]))
    site: str = best[0]
    return site


def _msrp_value(text: str) -> float | None:
    """The higher amount on the MSRP pages, as the spec 11.2 rule says."""
    if MSRP_HEADER not in text or MSRP_QUESTION not in text:
        return None
    section = text.split(MSRP_HEADER, 1)[1]
    amounts = [float(a) for a in _AMOUNT.findall(section)]
    return max(amounts) if amounts else None


@register_fake_handler("SelfAssessment")
def fake_self_assessment(request: FakeRequest) -> dict[str, Any]:
    text = request.text
    retail = _retail_choice(text)
    msrp = _msrp_value(text)
    return {
        "oe_choice": _oe_choice(text),
        "oe_confidence": FAKE_SELF_CONFIDENCE,
        "retail_choice": retail,
        "retail_confidence": FAKE_SELF_CONFIDENCE if retail else None,
        "msrp_value": msrp,
        "msrp_confidence": FAKE_SELF_CONFIDENCE if msrp is not None else None,
        "reasoning": "fake: followed the chain and the retail selection rules",
    }


@register_fake_handler("JudgeVerdict")
def fake_judge(request: FakeRequest) -> dict[str, Any]:
    return {"agree": True, "score": FAKE_JUDGE_SCORE, "concerns": ""}
