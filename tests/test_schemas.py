from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from rfq_agent import schemas
from rfq_agent.schemas import (
    SCHEMA_VERSION,
    ConfidenceResult,
    Contract,
    Evidence,
    MSRPResult,
    OECandidate,
    OEResolution,
    RetailResult,
    RFQLine,
    ShouldCostResult,
    WorksheetRow,
)

CONTRACTS = [
    obj
    for obj in vars(schemas).values()
    if isinstance(obj, type) and issubclass(obj, Contract) and obj is not Contract
]


def test_all_section7_models_present() -> None:
    names = {c.__name__ for c in CONTRACTS}
    assert names == {
        "RFQLine",
        "Evidence",
        "OECandidate",
        "OEResolution",
        "PriceQuote",
        "MSRPResult",
        "RetailResult",
        "CostBreakdown",
        "ShouldCostResult",
        "ConfidenceResult",
        "RowFailure",
        "PartSpec",
        "MaterialPrice",
        "MaterialList",
        "WorksheetRow",
        "ResolvedRow",
        "RunSpec",
        "RunRecord",
        "BatchRecord",
        "ReviewQueue",
        "ReviewPrompt",
        "ReviewInput",
        "ReviewDecision",
        "ReviewEntry",
        "RunLease",
        "LLMUsage",
        "RunProgress",
        "RunSummary",
        "RunDetail",
        "RowPage",
        "RowDetail",
        "ReviewOutcome",
        # evaluation (spec 14, P10)
        "OEEval",
        "MSRPEval",
        "SiteEval",
        "RetailEval",
        "ShouldCostEval",
        "BandEval",
        "ReliabilityBin",
        "CurvePoint",
        "ThresholdPick",
        "CalibrationEval",
        "AgentUsage",
        "UsageEval",
        "RowEval",
        "InjectionEval",
        "RunEval",
    }


@pytest.mark.parametrize("model", CONTRACTS, ids=lambda m: m.__name__)
def test_every_contract_has_schema_version(model: type[BaseModel]) -> None:
    field = model.model_fields["schema_version"]
    versioned = {OEResolution: "1.2", MSRPResult: "1.1", RetailResult: "1.1"}
    versioned[ShouldCostResult] = "1.1"
    versioned[OECandidate] = "1.1"
    versioned[WorksheetRow] = "1.1"
    versioned[schemas.RunRecord] = "1.1"
    versioned[schemas.ReviewDecision] = "1.1"
    versioned[schemas.RunSpec] = "1.1"
    versioned[schemas.RunEval] = "1.1"
    expected = versioned.get(model, SCHEMA_VERSION)
    assert field.default == expected
    assert SCHEMA_VERSION == "1.0"


def test_rfq_line_roundtrip() -> None:
    line = RFQLine(
        row_id=1,
        customer_part_no_raw=" ab-123 ",
        customer_part_no="AB123",
        our_part_no=None,
        description=None,
        annual_qty=500,
        customer_name="Acme",
    )
    assert RFQLine.model_validate_json(line.model_dump_json()) == line


def test_evidence_snippet_truncated_to_200() -> None:
    ev = Evidence(source="oem_catalog", ref="page-1", snippet="x" * 500)
    assert ev.snippet is not None and len(ev.snippet) == 200


def test_oe_resolution_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        OEResolution(
            row_id=1,
            status="maybe",  # type: ignore[arg-type]
            current_oe=None,
            supersession_chain=[],
            candidates=[],
            reasoning="",
        )


@pytest.mark.parametrize("score", [-1, 101])
def test_confidence_score_bounds(score: int) -> None:
    with pytest.raises(ValidationError):
        ConfidenceResult(
            row_id=1, score=score, band="red", strategy="evidence", signals={}, explanation=""
        )


def test_row_failure_contract() -> None:
    from rfq_agent.agents.common import RowFailure as CommonRowFailure
    from rfq_agent.schemas import RowFailure

    assert CommonRowFailure is RowFailure
    failure = RowFailure(row_id=3, stage="search", error="ToolTimeoutError: x")
    assert failure.schema_version == "1.0"


def test_oe_resolution_1_1_records_read_as_untruncated() -> None:
    old = {
        "schema_version": "1.1",
        "row_id": 3,
        "our_part_no": None,
        "our_part_source": "none",
        "status": "resolved",
        "current_oe": "NEW-3",
        "supersession_chain": ["OLD-1", "NEW-3"],
        "candidates": [],
        "reasoning": "",
    }
    parsed = OEResolution.model_validate(old)
    assert parsed.chain_truncated is False


def test_manual_review_contract_fields() -> None:
    from rfq_agent.schemas import OECandidate, WorksheetRow

    manual = OECandidate(oe_part_no="NEW-3", brand=None, match_type="manual", evidence=[])
    assert manual.schema_version == "1.1"
    assert WorksheetRow.model_fields["review_status"].default is None
    assert WorksheetRow.model_fields["schema_version"].default == "1.1"
