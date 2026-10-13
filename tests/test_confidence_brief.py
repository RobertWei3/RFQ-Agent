"""Self-assessment brief (spec 12.2): the evidence the LLM answers from, never the conclusion."""

from __future__ import annotations

from rfq_agent.confidence.brief import MSRP_HEADER, self_brief
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import Evidence, MSRPResult, ResolvedRow
from tests.builders import cost, line, msrp, oe, quote, retail


def row_with(msrp_result: MSRPResult) -> ResolvedRow:
    [row] = resolve_rows([line(1)], [oe(1)], [msrp_result], [retail(1)], [cost(1)])
    return row


def test_the_msrp_pages_are_shown_without_the_price_the_system_took() -> None:
    page = Evidence(source="msrp_lookup", ref="msrp-17", snippet="NEW-3 list price $112.40 USD")
    taken = quote("msrp_lookup", 123.45, "msrp", matched_part_no="NEW-3", evidence=[page])
    text = self_brief(row_with(msrp(1, quote=taken)))
    assert MSRP_HEADER in text
    assert 'msrp_lookup msrp-17 (for NEW-3): "NEW-3 list price $112.40 USD"' in text
    assert "123.45" not in text
    assert "found" not in text


def test_no_msrp_section_without_an_msrp_quote() -> None:
    assert MSRP_HEADER not in self_brief(row_with(msrp(1, None)))
