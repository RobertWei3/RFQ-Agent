"""Stage contracts (spec section 7). Stages exchange only these Pydantic models."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

MatchQuality = Literal["exact", "normalized", "fuzzy"]  # best first
MatchType = Literal["exact", "normalized", "interchange", "llm_inferred", "manual"]
StrategyName = Literal["evidence", "evidence_plus_self", "evidence_plus_judge"]
SNIPPET_MAX_CHARS = 200


class Contract(BaseModel):
    """Base class for every inter-stage contract."""

    schema_version: str = SCHEMA_VERSION


class RFQLine(Contract):
    row_id: int
    customer_part_no_raw: str
    customer_part_no: str = Field(description="Normalized customer part number.")
    our_part_no: str | None
    description: str | None
    annual_qty: int | None
    customer_name: str | None


class Evidence(Contract):
    source: str = Field(description='e.g. "oem_catalog", "retailer_3", "internal_db".')
    ref: str = Field(description="Page ID or record ID.")
    snippet: str | None = Field(
        default=None, description=f"Key source excerpt, at most {SNIPPET_MAX_CHARS} chars."
    )

    @field_validator("snippet")
    @classmethod
    def _truncate_snippet(cls, value: str | None) -> str | None:
        if value is None or len(value) <= SNIPPET_MAX_CHARS:
            return value
        return value[:SNIPPET_MAX_CHARS]


class OECandidate(Contract):
    schema_version: str = "1.1"  # 1.1 adds match_type "manual" (OE entered in review)
    oe_part_no: str
    brand: str | None
    match_type: MatchType
    evidence: list[Evidence]


class OEResolution(Contract):
    schema_version: str = "1.2"  # 1.1 adds our_part_no / our_part_source; 1.2 chain_truncated
    row_id: int
    our_part_no: str | None = Field(
        default=None, description="Our part number, given in the RFQ or found via xref."
    )
    our_part_source: Literal["given", "xref", "none"] = "none"
    status: Literal["resolved", "multiple", "not_found"]
    current_oe: str | None = Field(description="Current OE number at the end of the chain.")
    supersession_chain: list[str] = Field(description='e.g. ["OLD-1", "MID-2", "NEW-3"].')
    chain_truncated: bool = Field(
        default=False, description="Chain expansion hit the hop limit with numbers left to follow."
    )
    candidates: list[OECandidate] = Field(description="All candidates are kept.")
    reasoning: str


class PriceQuote(Contract):
    source: str
    price_type: Literal["msrp", "retail"]
    matched_part_no: str
    list_price: float | None
    sale_price: float | None
    discount_pct: float | None
    effective_price: float | None = Field(
        description="Sale price when discounted, otherwise list price."
    )
    currency: str = "USD"
    in_stock: bool | None
    match_quality: MatchQuality
    reasoning: str
    evidence: list[Evidence]


class MSRPResult(Contract):
    schema_version: str = "1.1"  # 1.1 adds reasoning
    row_id: int
    oe_part_no: str | None
    quote: PriceQuote | None
    status: Literal["found", "not_found", "discontinued"]
    reasoning: str = Field(default="", description="Why this result, incl. chain fallback.")


class RetailResult(Contract):
    schema_version: str = "1.1"  # 1.1 adds reasoning
    row_id: int
    oe_part_no: str | None
    quotes: list[PriceQuote] = Field(description="At most one quote per site.")
    selected: PriceQuote | None = Field(description="Quote used as the pricing reference.")
    outliers: list[str] = Field(description="Sources flagged as price outliers.")
    reasoning: str = Field(
        default="", description="Row conclusion: skip reason, failed sites, outliers, selection."
    )


class CostBreakdown(Contract):
    material: float
    conversion: float
    overhead: float
    sga: float
    profit: float
    packaging: float
    fob: float
    freight: float
    insurance: float
    duty: float
    mpf: float
    brokerage: float
    inland_freight: float
    landed_cost: float


SpecSource = Literal["both_agree", "internal", "external", "conflict", "missing"]
# Strongest first; a row's spec_source is its weakest field, "missing" overriding all
# (spec 11.4, same order as the confidence signal in 12.2).
SPEC_SOURCE_STRENGTH: tuple[SpecSource, ...] = ("both_agree", "internal", "external", "conflict")


def weakest_spec_source(sources: Sequence[SpecSource]) -> SpecSource:
    """Combine per-field sources into the row's ``spec_source``."""
    if "missing" in sources:
        return "missing"
    return max(sources, key=SPEC_SOURCE_STRENGTH.index)


class ShouldCostResult(Contract):
    schema_version: str = "1.1"  # 1.1 adds category / cycle_time_min / evidence
    row_id: int
    category: str | None = Field(
        default=None,
        description="Cost-model category used (internal DB, OEM catalog page or RFQ description).",
    )
    material: str | None = Field(description="Material key used for costing.")
    weight_kg: float | None = Field(description="Weight used for costing.")
    cycle_time_min: float | None = Field(
        default=None, description="Part cycle time, or the category default (see reasoning)."
    )
    spec_source: SpecSource
    breakdown: CostBreakdown | None
    status: Literal["estimated", "insufficient_data"]
    reasoning: str
    evidence: list[Evidence] = Field(default_factory=list)


class ConfidenceResult(Contract):
    row_id: int
    score: int = Field(ge=0, le=100)
    band: Literal["green", "yellow", "red"]
    strategy: str
    signals: dict[str, float] = Field(description="Evidence signals, kept for explainability.")
    explanation: str


class RowFailure(Contract):
    """A row an agent stage could not process; the rest of the batch continues (section 7)."""

    row_id: int
    stage: str = Field(description='Where it failed, e.g. "search_chain" or "upstream_oe".')
    error: str


# --- internal-DB records (MCP tool payloads, section 10) -------------------------------


class PartSpec(Contract):
    """``get_part_spec``: one of our parts; material or weight may be missing."""

    found: bool
    our_part_no: str
    category: str | None = None
    description: str | None = None
    material: str | None = None
    weight_kg: float | None = None
    cycle_time_min: float | None = None


class MaterialPrice(Contract):
    """``get_material_price``: current price per kg of one material key."""

    found: bool
    material: str
    price_per_kg: float | None = None
    currency: str | None = None
    as_of: str | None = None


class MaterialList(Contract):
    """``list_materials``: every material key with a price."""

    materials: list[str]


# --- worksheet (spec 12.1) ---------------------------------------------------------------

Band = Literal["green", "yellow", "red"]
ReviewStatus = Literal["accepted", "modified", "rejected"]


class WorksheetRow(Contract):
    """Flat per-row columns shared by the Excel sheet, row-list API and dashboard table."""

    schema_version: str = "1.1"  # 1.1 adds review_status

    row_id: int
    customer_part_no_raw: str
    customer_part_no: str
    our_part_no: str | None
    oe_status: Literal["resolved", "multiple", "not_found"] | None = Field(
        description="None when OE resolution failed."
    )
    current_oe: str | None
    supersession_chain: list[str]
    oe_candidates: list[str] = Field(
        description="Current OE of each grounded candidate (one-to-many); no llm_inferred ones."
    )
    shared_oe_rows: list[int] = Field(description="Other rows resolving to the same OE.")
    price_source_row: int | None = Field(
        description="Row whose price results are reused (many-to-one), if not this row."
    )
    shared_prices_disagree: bool = Field(
        default=False, description="Rows sharing this OE reached different price conclusions."
    )
    msrp: float | None
    msrp_status: Literal["found", "not_found", "discontinued"] | None = Field(
        description="None when the OE is not uniquely resolved (no price lookup)."
    )
    retail_prices: dict[str, float | None] = Field(description="Site -> effective price.")
    selected_retail_price: float | None
    selected_retail_source: str | None
    retail_outliers: list[str]
    should_cost_fob: float | None
    landed_cost: float | None
    confidence_score: int | None = None
    band: Band | None = None
    review_reasons: list[str] = Field(default_factory=list)
    failures: list[str] = Field(
        default_factory=list, description='"path:stage:error" for each failed path.'
    )
    reasoning_summary: dict[str, str] = Field(
        default_factory=dict, description="First 200 chars of each path's reasoning."
    )
    review_status: ReviewStatus | None = Field(
        default=None, description="Manual review outcome (spec 13); None until reviewed."
    )


class ResolvedRow(Contract):
    """One RFQ line with its worksheet columns and the full stage outcomes."""

    line: RFQLine
    worksheet: WorksheetRow
    oe: OEResolution | RowFailure
    msrp: MSRPResult | RowFailure = Field(description="Representative row's result if shared.")
    retail: RetailResult | RowFailure = Field(description="Representative row's result if shared.")
    should_cost: ShouldCostResult | RowFailure
    confidence: ConfidenceResult | None = None


# --- orchestrated runs and review (spec 13) ----------------------------------------------

BatchStatus = Literal["pending", "running", "completed", "failed"]
OPEN_BATCH_STATUSES: tuple[BatchStatus, ...] = ("pending", "running")  # resume runs these
RunStatus = Literal["running", "awaiting_review", "completed"]
ReviewAction = Literal["accept", "modify_oe", "modify_price", "reject"]


class RunSpec(Contract):
    """Run parameters fixed at creation; resume and rerun-failed reuse them."""

    schema_version: str = "1.1"  # 1.1 adds rescored_from
    input: str
    dataset_dir: str
    batch_size: int = Field(ge=1)
    strategy: StrategyName
    review: bool
    rescored_from: str | None = Field(
        default=None, description="Run whose stage outcomes this run re-scored (rfq rescore)."
    )


class RunRecord(Contract):
    schema_version: str = "1.1"  # 1.1 adds finished_at
    run_id: str
    created_at: str
    spec: RunSpec
    status: RunStatus = Field(
        description="awaiting_review: finalized, worksheet ready, red rows still to review."
    )
    finished_at: str | None = Field(
        default=None, description="When the latest finalize completed (elapsed time)."
    )


class BatchRecord(Contract):
    batch_id: int
    row_ids: list[int]
    rerun_rows: list[int] | None = Field(
        default=None,
        min_length=1,
        description="Rows a pending rerun will run; None (never empty) means the whole batch.",
    )
    status: BatchStatus
    attempts: int = Field(description="Executions started, a resumed batch counts again.")
    error: str | None = None

    def rows_to_run(self) -> list[int]:
        """Rows the next execution of this batch runs (spec 13)."""
        return self.rerun_rows if self.rerun_rows is not None else self.row_ids


class ReviewQueue(Contract):
    """Every red row still waiting for review (the ``interrupt`` value, spec 13)."""

    run_id: str
    row_ids: list[int]


class ReviewPrompt(Contract):
    """The next queued row, as the CLI reviewer is shown it."""

    run_id: str
    remaining: int = Field(description="Red rows still waiting, this one included.")
    row: ResolvedRow


class ReviewInput(Contract):
    """A reviewer's answer without the row (API request body; row id from the path)."""

    action: ReviewAction
    oe: str | None = None
    msrp: float | None = Field(default=None, gt=0)
    retail: float | None = Field(default=None, gt=0)
    note: str | None = None

    @model_validator(mode="after")
    def _check_inputs(self) -> ReviewInput:
        if self.action == "modify_oe" and not (self.oe or "").strip():
            raise ValueError("modify_oe needs an OE number")
        if self.action == "modify_price" and self.msrp is None and self.retail is None:
            raise ValueError("modify_price needs an MSRP or a reference retail price")
        return self


class ReviewDecision(ReviewInput):
    """An answer for one queued row (the ``Command(resume=...)`` value).

    A decision for a row no longer in the queue is ignored.
    """

    schema_version: str = "1.1"  # 1.1 adds row_id (the whole queue is put to review)
    row_id: int

    @classmethod
    def for_row(cls, row_id: int, answer: ReviewInput) -> ReviewDecision:
        return cls(row_id=row_id, **answer.model_dump(exclude={"schema_version"}))


class ReviewEntry(Contract):
    """One row of ``review_log``: the decision with values before and after."""

    row_id: int
    action: ReviewAction
    oe_before: str | None = None
    oe_after: str | None = None
    msrp_before: float | None = None
    msrp_after: float | None = None
    retail_before: float | None = None
    retail_after: float | None = None
    note: str | None = None
    snapshot: ResolvedRow = Field(description="The row as it was before the decision.")
    ts: str = ""


# --- API responses (spec 15.1) -------------------------------------------------------------

ReviewFilter = Literal["unreviewed", "accepted", "modified", "rejected"]
CoveragePath = Literal["oe", "msrp", "retail", "should_cost"]


class RunLease(Contract):
    """Who drives a run (spec 13 run lease): owner token and last heartbeat (epoch s)."""

    owner: str
    heartbeat: float

    def fresh(self, ttl_s: float, now: float | None = None) -> bool:
        """The heartbeat is recent enough that nobody may take the run over."""
        return self.heartbeat >= (time.time() if now is None else now) - ttl_s


class LLMUsage(Contract):
    """LLM calls of one run, from ``llm_calls``."""

    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = Field(description="None when a successful call has no price.")


class RunProgress(Contract):
    """Batches by status, read from ``batch_results``."""

    batches_total: int
    pending: int
    running: int
    completed: int
    failed: int


class RunSummary(Contract):
    """Run totals: the worksheet's second sheet and ``GET /runs/{id}`` (spec 12.3)."""

    rows: int
    bands: dict[Band, int] = Field(description="Row counts per band.")
    reviews: dict[ReviewStatus, int] = Field(description="Row counts per review status.")
    review_pending: int = Field(description="Red rows still waiting for review.")
    coverage: dict[CoveragePath, float] = Field(
        description="Share of rows the system covered: oe, msrp, retail, should_cost "
        "(manual prices not counted)."
    )
    rows_with_failure: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    llm_cost_usd: float | None = Field(description="None when a model has no price.")
    elapsed_s: float | None = Field(
        description="finished_at - created_at, wall clock (includes time a killed run was down)."
    )


class RunDetail(Contract):
    run: RunRecord
    progress: RunProgress
    summary: RunSummary | None = Field(description="None until the run is finalized.")
    lease_active: bool = Field(description="A process is driving the run right now.")


class RowPage(Contract):
    total: int = Field(description="Rows matching the filters, all pages.")
    offset: int
    limit: int
    items: list[WorksheetRow]


class RowDetail(Contract):
    row: ResolvedRow
    reviews: list[ReviewEntry]
    in_review_queue: bool


class ReviewOutcome(Contract):
    run_id: str
    row_id: int
    done: bool = Field(description="False: accepted, a worker finishes it (HTTP 202).")
    row: ResolvedRow | None = Field(default=None, description="The row after review, when done.")


# --- evaluation (spec 14, P10) ---------------------------------------------------------------


InjectionKind = Literal["oe", "msrp", "retail"]


class OEEval(Contract):
    coverage: float = Field(description="Resolved rows / all rows.")
    accuracy: float | None = Field(description="Resolved rows whose current OE is the primary.")
    conclusion_accuracy: float = Field(
        description="All rows: OE conclusion right, incl. correct not_found / multiple."
    )
    chain_accuracy: float | None = Field(
        description="Rows resolved by both system and truth whose chain matches exactly."
    )


class MSRPEval(Contract):
    coverage: float = Field(description="found or discontinued / all rows.")
    accuracy: float | None = Field(description="Priced rows within 1% of the true MSRP.")
    status_accuracy: float
    mean_rel_error: float | None


class SiteEval(Contract):
    expected: int = Field(description="Rows where truth has a listing on the site.")
    quoted: int = Field(description="Rows with a quote from the site.")
    found: int = Field(description="Expected rows that got a quote from the site.")
    coverage: float | None = Field(description="found / expected.")


class RetailEval(Contract):
    sites: dict[str, SiteEval]
    selected_accuracy: float = Field(description="Reference site right (none when none due).")
    outlier_precision: float | None = Field(description="Per (row, site); truth-resolved rows.")
    outlier_recall: float | None


class ShouldCostEval(Contract):
    spec_accuracy: float = Field(description="Category, material and weight (<= 1%).")
    landed_mape_part: float | None = Field(description="Rows with the part's own cycle time.")
    landed_mape_default: float | None = Field(description="Rows with the category default.")
    n_part: int
    n_default: int


class BandEval(Contract):
    rows: int
    usable: int = Field(description="Rows usable as is.")
    row_correct: int


class ReliabilityBin(Contract):
    lo: int = Field(description="Scores in [lo, hi); the last bin includes 100.")
    hi: int
    rows: int
    mean_score: float | None = Field(description="Mean of score / 100.")
    usable_rate: float | None


class CurvePoint(Contract):
    threshold: int = Field(description="Only rows scoring >= threshold are handled automatically.")
    coverage: float
    accuracy: float | None = Field(description="Share usable as is among those rows.")


class ThresholdPick(Contract):
    target: float = Field(description="Target accuracy.")
    threshold: int | None = Field(description="Lowest threshold reaching the target.")
    coverage: float | None


class CalibrationEval(Contract):
    row_correct_rate: float
    usable_rate: float
    ece: float
    auroc: float | None = Field(description="None when every row has the same label.")
    bands: dict[Band, BandEval]
    red_row_correct_rate: float | None = Field(
        description="Red rows that are correct: rows rightly held back."
    )
    reliability: list[ReliabilityBin]
    curve: list[CurvePoint]
    thresholds: list[ThresholdPick]


class AgentUsage(Contract):
    agent: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = Field(description="None when a successful call has no price.")


class UsageEval(Contract):
    agents: list[AgentUsage]
    models: dict[str, str] = Field(description="Role -> provider/model seen in llm_calls.")
    cost_usd: float | None = Field(description="This run's own calls.")
    source_cost_usd: float | None = Field(
        description="Re-scored run: the source run's calls (stage agents); else None."
    )
    total_cost_usd: float | None
    elapsed_s: float | None
    cost_per_1k_rows: float | None


class RowEval(Contract):
    row_id: int
    score: int | None
    band: Band | None
    row_correct: bool
    usable: bool = Field(description="Usable as is (spec 12.2).")


class InjectionEval(Contract):
    """How the scores treated rows made wrong on purpose (spec 14 injection experiment)."""

    rows: int = Field(description="Injected rows.")
    bands_by_kind: dict[InjectionKind, dict[Band, int]] = Field(
        description="Injected rows per error kind and band."
    )
    held_back_rate: float | None = Field(description="Injected rows in yellow or red.")
    green_injected: int = Field(description="Injected rows in green: wrong rows used as is.")
    auroc: float | None = Field(description="Usable non-injected rows vs injected rows.")


class RunEval(Contract):
    """A run compared with ground truth (spec 14); ``GET /runs/{id}/eval``."""

    schema_version: str = "1.1"  # 1.1 adds injection
    run_id: str
    created_at: str
    strategy: StrategyName
    truth_path: str
    rows: int
    reviewed_rows: int = Field(description="Rows judged on their pre-review system result.")
    oe: OEEval
    msrp: MSRPEval
    retail: RetailEval
    should_cost: ShouldCostEval
    calibration: CalibrationEval
    usage: UsageEval
    row_evals: list[RowEval]
    injection: InjectionEval | None = Field(
        default=None, description="Only for a run with injected errors."
    )
