"""Evidence signals, score, gates and bands (spec 12.2), pure functions."""

from __future__ import annotations

from typing import Any

import pytest

from rfq_agent.confidence.evidence import (
    apply_gates,
    band_for,
    evidence_score,
    evidence_signals,
    gates,
)
from rfq_agent.config import Settings
from rfq_agent.resolution import resolve_rows
from rfq_agent.schemas import OECandidate, ResolvedRow, RowFailure
from tests.builders import cost, line, msrp, oe, retail


def one_row(**overrides: Any) -> ResolvedRow:
    parts: dict[str, Any] = {
        "oe": oe(1),
        "msrp": msrp(1),
        "retail": retail(1),
        "cost": cost(1),
    }
    parts.update(overrides)
    [row] = resolve_rows(
        [line(1)], [parts["oe"]], [parts["msrp"]], [parts["retail"]], [parts["cost"]]
    )
    return row


# --- signals ------------------------------------------------------------------------------


def test_signals_of_a_clean_row(settings: Settings) -> None:
    signals = evidence_signals(one_row(), settings)

    # retail 80 / 82 / 85: mean 82.333, population sd 2.0548, cv 0.024957 -> 1 - cv / 0.40
    assert signals == pytest.approx(
        {
            "oe_match": 0.7,  # interchange
            "chain": 1.0,
            "price_sources": 1.0,  # MSRP + 3 quotes, capped at 4
            "retail_dispersion": 0.93761,
            "msrp_retail": 1.0,  # 80 / 100
            "spec_source": 1.0,  # both_agree
        },
        abs=1e-4,
    )


def test_clean_row_scores_92(settings: Settings) -> None:
    # 100 * (0.25*0.7 + 0.10 + 0.20 + 0.15*0.93761 + 0.10 + 0.20) = 91.56
    assert evidence_score(evidence_signals(one_row(), settings), settings) == 92


def test_best_candidate_match_type_counts(settings: Settings) -> None:
    candidates = [
        OECandidate(oe_part_no="NEW-3", brand=None, match_type=kind, evidence=[])
        for kind in ("llm_inferred", "normalized")
    ]
    row = one_row(oe=oe(1, candidates=candidates))
    assert evidence_signals(row, settings)["oe_match"] == 0.9


def test_truncated_chain_lowers_the_chain_signal(settings: Settings) -> None:
    row = one_row(oe=oe(1, chain_truncated=True))
    assert evidence_signals(row, settings)["chain"] == 0.3


def test_outliers_do_not_count_as_price_sources(settings: Settings) -> None:
    quotes = retail(1, {"retail_1": 80.0, "retail_2": 200.0})
    quotes.outliers = ["retail_2"]
    signals = evidence_signals(one_row(msrp=msrp(1, None), retail=quotes), settings)
    assert signals["price_sources"] == 0.25  # one usable quote, no MSRP
    assert signals["retail_dispersion"] == 0.5  # fewer than two usable quotes


@pytest.mark.parametrize(
    ("msrp_value", "expected"),
    [(80.0, 1.0), (100.0, 1.0)],  # retail 80: ratio 1.0 and 0.8
)
def test_retail_at_or_below_msrp_is_plausible(
    settings: Settings, msrp_value: float, expected: float
) -> None:
    row = one_row(msrp=msrp(1, msrp_value))
    assert evidence_signals(row, settings)["msrp_retail"] == expected


@pytest.mark.parametrize(("msrp_value", "expected"), [(72.0, 0.5), (66.0, 0.0)])
def test_retail_above_msrp_is_suspicious(
    settings: Settings, msrp_value: float, expected: float
) -> None:
    # selected retail is 80: 80 / 72 = 1.11 (<= 1.2), 80 / 66 = 1.21 (> 1.2)
    row = one_row(msrp=msrp(1, msrp_value))
    assert evidence_signals(row, settings)["msrp_retail"] == expected


def test_missing_msrp_makes_the_ratio_neutral(settings: Settings) -> None:
    assert evidence_signals(one_row(msrp=msrp(1, None)), settings)["msrp_retail"] == 0.5


@pytest.mark.parametrize(
    ("source", "expected"),
    [("internal", 0.8), ("external", 0.5), ("conflict", 0.3)],
)
def test_spec_source_levels(settings: Settings, source: str, expected: float) -> None:
    row = one_row(cost=cost(1, spec_source=source))
    assert evidence_signals(row, settings)["spec_source"] == expected


def test_failed_should_cost_gives_no_spec_signal(settings: Settings) -> None:
    row = one_row(cost=RowFailure(row_id=1, stage="internal_spec", error="mcp down"))
    assert evidence_signals(row, settings)["spec_source"] == 0.0


# --- gates and bands ----------------------------------------------------------------------


def multiple_oe() -> object:
    cands = [
        OECandidate(oe_part_no=pn, brand=None, match_type="interchange", evidence=[])
        for pn in ("A-1", "B-2")
    ]
    return oe(1, status="multiple", current_oe=None, supersession_chain=[], candidates=cands)


@pytest.mark.parametrize(
    ("overrides", "gate"),
    [
        ({"oe": "multiple"}, "gate_oe_multiple"),
        ({"oe": oe(1, current=None)}, "gate_oe_not_found"),
        ({"msrp": RowFailure(row_id=1, stage="search_chain", error="t")}, "gate_failure"),
        ({"retail": "all_outliers"}, "gate_all_outliers"),
    ],
)
def test_red_gates_cap_at_49(settings: Settings, overrides: dict[str, object], gate: str) -> None:
    if overrides.get("oe") == "multiple":
        overrides = {"oe": multiple_oe(), "msrp": msrp(1, None), "retail": retail(1, {})}
    if overrides.get("retail") == "all_outliers":
        every = retail(1, {"retail_1": 10.0, "retail_2": 90.0}, selected=None)
        every.outliers = ["retail_1", "retail_2"]
        overrides = {"retail": every}
    caps = gates(one_row(**overrides), settings)
    assert caps == {gate: 49}
    assert apply_gates(92, caps) == 49
    assert apply_gates(30, caps) == 30


def test_insufficient_should_cost_caps_at_79(settings: Settings) -> None:
    row = one_row(cost=cost(1, status="insufficient_data", spec_source="missing", breakdown=None))
    assert gates(row, settings) == {"gate_insufficient_data": 79}


def test_shared_rows_with_differing_prices_cap_at_79(settings: Settings) -> None:
    rows = resolve_rows(
        [line(1), line(2)],
        [oe(1), oe(2)],
        [msrp(1, 100.0), msrp(2, 120.0)],
        [retail(1), retail(2)],
        [cost(1), cost(2)],
    )
    assert gates(rows[1], settings) == {"gate_shared_disagree": 79}


def test_clean_row_has_no_gates(settings: Settings) -> None:
    assert gates(one_row(), settings) == {}
    assert apply_gates(92, {}) == 92


@pytest.mark.parametrize(
    ("score", "band"), [(100, "green"), (80, "green"), (79, "yellow"), (50, "yellow"), (49, "red")]
)
def test_bands(settings: Settings, score: int, band: str) -> None:
    assert band_for(score, settings) == band


# --- review fixes ------------------------------------------------------------------------


def test_ratio_exactly_at_the_suspicious_limit_is_still_half(settings: Settings) -> None:
    row = one_row(msrp=msrp(1, 50.0), retail=retail(1, {"retail_1": 60.0, "retail_2": 61.0}))
    assert evidence_signals(row, settings)["msrp_retail"] == 0.5  # 60 / 50 = 1.2


def test_suspicious_ratio_comes_from_config(settings: Settings) -> None:
    settings.confidence.evidence.msrp_retail_suspicious_ratio = 1.5
    row = one_row(msrp=msrp(1, 66.0))  # 80 / 66 = 1.21
    assert evidence_signals(row, settings)["msrp_retail"] == 0.5


def test_dispersion_never_goes_negative(settings: Settings) -> None:
    # 10 and 100: cv = 45 / 55 = 0.82, above the 0.40 threshold
    row = one_row(retail=retail(1, {"retail_1": 10.0, "retail_2": 100.0}))
    assert evidence_signals(row, settings)["retail_dispersion"] == 0.0


def test_discontinued_msrp_with_a_price_counts_as_a_source(settings: Settings) -> None:
    row = one_row(msrp=msrp(1, status="discontinued"), retail=retail(1, {}))
    assert evidence_signals(row, settings)["price_sources"] == 0.25


def test_red_gates_are_known_by_name_not_by_value() -> None:
    from rfq_agent.confidence.evidence import has_red_gate

    # A yellow gate stays yellow even if its cap happens to equal the red cap.
    assert not has_red_gate({"gate_insufficient_data": 49})
    assert has_red_gate({"gate_oe_multiple": 49})


def test_an_unclassified_gate_is_an_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rfq_agent.confidence import evidence

    monkeypatch.setattr(evidence, "_triggered_gates", lambda row: ["gate_brand_new"])
    with pytest.raises(KeyError, match="gate_brand_new"):
        gates(one_row(), settings)
