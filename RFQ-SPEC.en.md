# RFQ-Agent Implementation Spec (for Claude Code)

[中文](RFQ-SPEC.md) | English

> **This is a translation. The Chinese [RFQ-SPEC.md](RFQ-SPEC.md) is authoritative; if the two differ, the Chinese version wins. Please report differences in a GitHub issue.** Terms follow [GLOSSARY.md](GLOSSARY.md); see the [Chinese–English term table](#appendix-chineseenglish-terms) at the end. "I" / "me" in this document is the project owner.

> Repository directory: `RFQ-Agent/`
> This document is the single source of requirements. Implement in the Phase order of section 17, and **stop after each Phase** to report what was done, the test results and the plan for the next step; wait for my confirmation before continuing.

---

## 0. Working rules for Claude Code

1. Follow the Phase order strictly; do not implement ahead across Phases.
2. Every Phase must have pytest tests, and all of them must pass in **fake LLM mode** (no API cost).
3. Smoke tests that call a real LLM are limited to ≤20 parts per run; tell me the expected number of calls before running.
4. Every external data source is a mock. Do not write any crawler for a real website.
5. When unsure about a third-party API (LangGraph, MCP SDK, langchain-mcp-adapters, DeepSeek model IDs), check the official documentation first instead of writing from memory.
6. When a business question is not settled by this document, **stop and ask me**; do not invent business rules.
7. Code style: complete type annotations, Pydantic v2, short functions, and modules interact only through the schemas defined in this document.
8. **All code comments and docstrings must be in English**; no Chinese comments.

---

## 1. Background and goals

This is a **project for learning and exchange**. The reference system is an RFQ (request for quotation) auto-quoting workflow built on an agent platform and used in production; this project builds a simplified version from scratch with Python + LangGraph, in order to:

- Fully understand the original design: splitting work across agents, stage contracts, entity resolution, batching and fault tolerance, confidence-based triage, human review.
- Compare the trade-offs of the "platform" approach and the "self-built" approach.
- Test the design choices with measurable evaluation results (coverage, accuracy, confidence calibration, cost).

**Business flow**: a customer sends an Excel RFQ (hundreds to thousands of customer part numbers) → the system finds the OE (original equipment) number for each part number → looks up the OE maker's suggested retail price (MSRP) and retail prices → estimates the should-cost → outputs a pricing worksheet for the pricing team to decide on.

## 2. Non-goals

- No real crawlers, no connection to a real Azure SQL.
- No user login or multi-tenancy.
- The first front end is Streamlit; it will later be replaced by React, so **the front end may access the back end only through FastAPI** and must not import back-end modules directly.

---

## 3. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Dependency management | uv |
| Agent orchestration | LangGraph (StateGraph, parallel fan-out, checkpointer, interrupt) |
| LLM access | LangChain chat model abstraction |
| Default LLM | DeepSeek (OpenAI-compatible API; `base_url` and model ID live in the config; check the official docs for the current model ID during implementation) |
| LLM for tests | A home-made FakeLLM (deterministic, zero cost, see §8) |
| Internal data | SQLite (standing in for Azure SQL), exposed through an **MCP server** (official Python `mcp` SDK) |
| MCP client | `langchain-mcp-adapters` |
| Structured output | Pydantic v2 + model structured output; automatic retry on validation failure |
| Persistence | SQLite (business result tables + LangGraph checkpoints) |
| API | FastAPI |
| Dashboard | Streamlit (calls FastAPI only) |
| Excel | openpyxl (colored by confidence band) |
| Tests | pytest |
| Configuration | YAML + `.env` (API keys only in `.env`, and `.env` is in `.gitignore`) |

---

## 4. Architecture overview

```
Excel RFQ
   │
   ▼
[intake] deterministic parsing and part-number normalization
   │
   ▼
[oe_interchange agent] customer part number → OE number (with old-to-new supersession chain)
   │
   ├──────────────┬───────────────┐   ← parallel fan-out
   ▼              ▼               ▼
[oe_msrp agent] [retail_price agent] [should_cost agent]
   │              │               │
   └──────────────┴───────────────┘
   ▼
[entity_resolution] many-to-many resolution, merged into one record per row (deterministic)
   │
   ▼
[confidence] 0–100 confidence + green/yellow/red band (switchable strategy)
   │
   ▼
[output] result database, pricing worksheet Excel (downloadable once finalized) → FastAPI → Dashboard
   │
   ▼
[review] (--review mode only) review red rows on a finalized run (interrupt); after each decision go back to entity resolution and re-summarize; the worksheet follows
```

- Four subagents: `oe_interchange`, `oe_msrp`, `retail_price`, `should_cost`. Each can **run and be tested on its own** (with its own CLI subcommand); the orchestrator then chains them.
- The orchestrator runs the four agents **in batches**: 100 rows per batch; a batch finishes and its results are stored before the next batch starts; after all batches, entity resolution, confidence and human review run over the whole table (see §13).

---

## 5. Directory layout

```
RFQ-Agent/
├── RFQ-SPEC.md                # requirements and business rules (Chinese, the only authoritative version)
├── RFQ-SPEC.en.md             # English translation; the Chinese version wins (§16)
├── GLOSSARY.md                # domain terms
├── README.md                  # English
├── README.zh-CN.md            # Chinese
├── LICENSE                    # MIT
├── CLAUDE.md                  # workflow for Claude Code
├── pyproject.toml
├── .env.example
├── config/
│   ├── settings.yaml          # LLM, batch size, concurrency, thresholds, confidence strategy
│   └── cost_model.yaml        # illustrative rates for should-cost and landed cost
├── data/
│   ├── generated/             # synthetic data (generator output, ignored by git)
│   └── samples/               # small sample RFQs (committed)
├── src/rfq_agent/
│   ├── schemas.py             # all Pydantic contracts (§7)
│   ├── config.py
│   ├── llm/
│   │   ├── factory.py         # returns DeepSeek / Claude / Qwen / Fake per config
│   │   ├── fake.py            # FakeLLM
│   │   └── tracking.py        # callback recording tokens, cost, latency
│   ├── data_gen/              # synthetic data and ground-truth generator (§6)
│   ├── tools/                 # mock external data sources (§9)
│   ├── mcp_server/            # internal-database MCP server (§10)
│   ├── intake/
│   ├── agents/
│   │   ├── oe_interchange.py
│   │   ├── oe_msrp.py
│   │   ├── retail_price.py
│   │   └── should_cost.py
│   ├── resolution/            # entity resolution
│   ├── confidence/            # the three confidence strategies
│   ├── orchestrator/          # LangGraph main graphs, batching, checkpoints, review
│   ├── output/                # Excel writer
│   ├── store/                 # SQLite result database access layer
│   ├── api/                   # FastAPI
│   ├── eval/                  # evaluation scripts (§14)
│   ├── costing/               # should-cost and landed-cost formulas (deterministic, §11.4)
│   ├── partno.py              # part-number normalization (shared by intake, tools, MCP, matching)
│   ├── pricing.py             # tolerance for judging MSRP (shared by confidence and evaluation)
│   ├── trace.py               # local run trace (one JSONL line per step)
│   └── cli/                   # Typer CLI entry point, one module per command group
├── dashboard/                 # Streamlit; calls the API over HTTP only, never imports rfq_agent (P9)
│   ├── app.py                 # entry: page navigation, run picker in the sidebar
│   ├── api_client.py          # API client wrapping httpx
│   ├── charts.py              # pure functions: JSON → tables and chart data
│   └── views/                 # one module per page
├── docs/
│   └── design-notes.md        # design-notes template (§16)
└── tests/
```

---

## 6. Synthetic data

### 6.1 Generator

`rfq data generate --n-parts 200 --seed 42`, scalable to `--n-parts 8000` for batch stress tests. Every random process uses a fixed seed and is reproducible.

Categories: **thermostat** and **water pump**; all brands and vehicle models are fictional.

### 6.2 Generated entities

| Entity | Description |
|---|---|
| `our_parts` | Our part master data: `our_part_no`, description, category, material, weight in kg, cycle time (internal database, accessed through MCP) |
| `customer_xref` | Known customer part number ↔ our part number pairs (some rows have one, some do not) |
| `oem_catalog_pages` | **Text pages** simulating an OEM catalog: OE numbers, fitting vehicles, "superseded by / replaces" relations, with noise (extra text, inconsistent formatting) |
| `interchange_pages` | Simulated interchange lookup results: given a part number, text snippets with several candidate OE numbers |
| `msrp_pages` | Simulated price pages on the OE maker's website: OE number, MSRP, currency, sometimes marked "discontinued" |
| `retail_listings` | Product listing text from 5 fictional retail sites: title, part number (possibly written differently), list price, sale price, in stock or not |
| `material_prices` | Material price index (e.g. aluminum, cast iron, rubber, in $/kg) |
| `ground_truth` | The correct answer for each customer part number: correct current OE number, full supersession chain, correct MSRP, correct price at each site, true material and weight |

### 6.3 Deliberately planted difficulties (ratios adjustable in the generator parameters)

| Difficulty | Default ratio |
|---|---|
| Supersession chain (length 2–3, old number → new number) | 15% |
| One customer part number maps to several OE numbers (one-to-many) | 10% |
| Several customer part numbers map to the same OE number (many-to-one) | 10% |
| No OE number found | 8% |
| Some retail sites have no price | 15% |
| One site's price is a clear outlier (price conflict) | 10% |
| Part number written differently in retail titles (case, hyphens, spaces) | 20% |
| Internal database lacks material or weight, which must be extracted from external pages | 15% |
| Internal data disagrees with external pages | 5% |
| Messy input sheet (extra spaces, full-width characters, blank rows) | 5% |

### 6.4 RFQ input format

Excel, single sheet. Column names may differ in case and spacing; intake normalizes them:

| Column | Required | Description |
|---|---|---|
| `customer_part_no` | Yes | Customer part number |
| `our_part_no` | No | Filled when the customer already gives our matching part number, otherwise blank |
| `description` | No | Description, often missing |
| `annual_qty` | No | Annual quantity |
| `customer_name` | No | Customer name |

The generator also writes `data/samples/rfq_sample_20.xlsx` (20 rows) and `data/generated/rfq_200.xlsx`.

---

## 7. Data contracts (`schemas.py`)

Stages pass **only Pydantic objects** to each other. Every model has a `schema_version: str` field defaulting to `"1.0"`; a model whose fields were added or changed gets its own version bump (currently `OEResolution` is `"1.2"`; `OECandidate`, `MSRPResult`, `RetailResult`, `ShouldCostResult`, `WorksheetRow`, `RunRecord`, `ReviewDecision`, `RunSpec` and `RunEval` are `"1.1"`; see the comment on each model).

```python
class RFQLine(BaseModel):
    row_id: int
    customer_part_no_raw: str
    customer_part_no: str            # after normalization
    our_part_no: str | None
    description: str | None
    annual_qty: int | None
    customer_name: str | None

class Evidence(BaseModel):
    source: str                      # e.g. "oem_catalog", "retailer_3", "internal_db"
    ref: str                         # page ID or record ID
    snippet: str | None              # key source-text snippet (≤200 characters)

class OECandidate(BaseModel):     # schema_version "1.1" (P7 added manual: an OE number entered in human review)
    oe_part_no: str
    brand: str | None
    match_type: Literal["exact", "normalized", "interchange", "llm_inferred", "manual"]
    evidence: list[Evidence]

class OEResolution(BaseModel):     # schema_version "1.2" (P2 added our_part_no / our_part_source; P6 added chain_truncated)
    row_id: int
    our_part_no: str | None = None   # our part number: given in the RFQ or found through the MCP cross-reference
    our_part_source: Literal["given", "xref", "none"] = "none"
    status: Literal["resolved", "multiple", "not_found"]
    current_oe: str | None           # the currently valid OE number reached along the supersession chain
    supersession_chain: list[str]    # e.g. ["OLD-1", "MID-2", "NEW-3"]
    chain_truncated: bool = False    # numbers were still unfollowed when chain completion hit the hop limit (a confidence signal)
    candidates: list[OECandidate]    # all candidates are kept
    reasoning: str                   # the LLM's reasoning for its judgment

class PriceQuote(BaseModel):
    source: str
    price_type: Literal["msrp", "retail"]
    matched_part_no: str
    list_price: float | None
    sale_price: float | None
    discount_pct: float | None
    effective_price: float | None    # the sale price if there is one, otherwise the list price
    currency: str = "USD"
    in_stock: bool | None
    match_quality: Literal["exact", "normalized", "fuzzy"]
    reasoning: str
    evidence: list[Evidence]

class MSRPResult(BaseModel):       # schema_version "1.1" (P3 added reasoning)
    row_id: int
    oe_part_no: str | None
    quote: PriceQuote | None
    status: Literal["found", "not_found", "discontinued"]
    reasoning: str = ""              # reason for the conclusion, including supersession-chain fallback, why it was not looked up, etc.

class RetailResult(BaseModel):     # schema_version "1.1" (P4 added reasoning)
    row_id: int
    oe_part_no: str | None
    quotes: list[PriceQuote]         # at most one per site (the most precise match)
    selected: PriceQuote | None      # the one used as the pricing reference
    outliers: list[str]              # sources judged to be outliers
    reasoning: str = ""              # whole-row conclusion: why not looked up, site search failures, outlier deviation, why selected, etc.

class CostBreakdown(BaseModel):
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

class ShouldCostResult(BaseModel): # schema_version "1.1" (P5 added category / cycle_time_min / evidence)
    row_id: int
    category: str | None = None      # category used: internal database, or from the OEM catalog page / RFQ description when our part number is unknown
    material: str | None             # material key actually used (the internal value on conflict)
    weight_kg: float | None          # weight actually used (the internal value on conflict)
    cycle_time_min: float | None = None  # the part's own value or the category default (stated in reasoning)
    spec_source: Literal["internal", "external", "both_agree", "conflict", "missing"]
    breakdown: CostBreakdown | None
    status: Literal["estimated", "insufficient_data"]
    reasoning: str
    evidence: list[Evidence] = []    # internal database record, external spec pages, catalog pages

class ConfidenceResult(BaseModel):
    row_id: int
    score: int                       # 0–100
    band: Literal["green", "yellow", "red"]
    strategy: str
    signals: dict[str, float]        # each evidence signal, for explanation
    explanation: str

class RowFailure(BaseModel):
    # Uniform output when a row cannot be processed at some stage (no exception; other rows go on)
    row_id: int
    stage: str                       # stage that failed, e.g. "search_chain", "upstream_oe"
    error: str                       # reason for the failure

# Return values of the internal-database MCP tools (§10; formal contract since P5, shared by agents and the MCP server)
class PartSpec(BaseModel):           # get_part_spec
    found: bool
    our_part_no: str
    category: str | None = None
    description: str | None = None
    material: str | None = None      # empty when the master data is incomplete
    weight_kg: float | None = None
    cycle_time_min: float | None = None

class MaterialPrice(BaseModel):      # get_material_price; internal prices must state the currency, only USD is used
    found: bool
    material: str
    price_per_kg: float | None = None
    currency: str | None = None
    as_of: str | None = None

class MaterialList(BaseModel):       # list_materials
    materials: list[str]

class WorksheetRow(BaseModel):     # schema_version "1.1" (P7 added review_status)
    # Flat column model shared by Excel, the row-list API and the dashboard table; columns in §12.1
    row_id: int
    customer_part_no_raw: str
    customer_part_no: str
    our_part_no: str | None
    oe_status: Literal["resolved", "multiple", "not_found"] | None   # empty when OE is a row failure
    current_oe: str | None
    supersession_chain: list[str]
    oe_candidates: list[str]         # one-to-many: current number of each candidate (only those grounded in source text, no llm_inferred)
    shared_oe_rows: list[int]        # other rows sharing the same OE
    price_source_row: int | None     # which row the price results come from (many-to-one reuse)
    shared_prices_disagree: bool     # rows sharing the OE reached different price conclusions
    msrp: float | None
    msrp_status: Literal["found", "not_found", "discontinued"] | None   # empty when OE is not resolved
    retail_prices: dict[str, float | None]   # site → effective_price
    selected_retail_price: float | None
    selected_retail_source: str | None
    retail_outliers: list[str]
    should_cost_fob: float | None
    landed_cost: float | None
    confidence_score: int | None     # empty before confidence is computed
    band: Literal["green", "yellow", "red"] | None
    review_reasons: list[str]
    failures: list[str]              # "path:stage:reason"
    reasoning_summary: dict[str, str]        # first 200 characters of each path's reasoning
    review_status: Literal["accepted", "modified", "rejected"] | None = None   # human review outcome (§13)

class ResolvedRow(BaseModel):
    # Complete result of one row: used by row detail and confidence scoring
    line: RFQLine
    worksheet: WorksheetRow
    oe: OEResolution | RowFailure
    msrp: MSRPResult | RowFailure    # many-to-one: the representative row's result
    retail: RetailResult | RowFailure
    should_cost: ShouldCostResult | RowFailure
    confidence: ConfidenceResult | None = None

# Orchestrated runs and human review (§13, added in P7); shared by the CLI, the orchestrator and later the API
class RunSpec(BaseModel):            # parameters fixed when a run is created, reused by resume / rerun-failed; schema_version "1.1" (P10 added rescored_from)
    input: str
    dataset_dir: str
    batch_size: int
    strategy: Literal["evidence", "evidence_plus_self", "evidence_plus_judge"]
    review: bool
    rescored_from: str | None = None # copied runs (`rfq rescore`, `rfq eval inject`): whose four-path results were copied (§13)

class RunRecord(BaseModel):
    run_id: str
    created_at: str
    spec: RunSpec
    status: Literal["running", "awaiting_review", "completed"]   # awaiting_review: finalized, worksheet downloadable, red rows still awaiting review
    finished_at: str | None = None   # P8: when the latest finalize completed (for elapsed time in the summary)

class BatchRecord(BaseModel):
    batch_id: int
    row_ids: list[int]
    rerun_rows: list[int] | None = None   # a pending rerun runs only these rows (never an empty list); None means the whole batch
    status: Literal["pending", "running", "completed", "failed"]
    attempts: int                    # times execution started (resuming after an interruption counts too)
    error: str | None = None

    def rows_to_run(self) -> list[int]:   # rows for the next execution: rerun_rows, or all row_ids when it is None
        ...

class ReviewQueue(BaseModel):        # P8: the interrupt() value, the whole review queue at once
    run_id: str
    row_ids: list[int]               # red, unreviewed rows, by row_id

class ReviewPrompt(BaseModel):       # input of the CLI review callback: the next row in the queue
    run_id: str
    remaining: int                   # red rows still awaiting review, this one included
    row: ResolvedRow

class ReviewInput(BaseModel):        # P8: request body for submitting a review through the API (row id comes from the path)
    action: Literal["accept", "modify_oe", "modify_price", "reject"]
    oe: str | None = None            # required for modify_oe
    msrp: float | None = None        # modify_price needs at least one, > 0
    retail: float | None = None
    note: str | None = None

class ReviewDecision(ReviewInput):   # the Command(resume=...) value: a decision on one row of the queue
    row_id: int                      # added in P8; decisions on rows not in the current queue are ignored

class ReviewEntry(BaseModel):        # one row of review_log
    row_id: int
    action: Literal["accept", "modify_oe", "modify_price", "reject"]
    oe_before: str | None = None
    oe_after: str | None = None
    msrp_before: float | None = None
    msrp_after: float | None = None
    retail_before: float | None = None
    retail_after: float | None = None
    note: str | None = None
    snapshot: ResolvedRow            # the whole row before review
    ts: str = ""                     # when written to review_log; filled with the current time by the result database when empty

# Run lease and LLM usage (added in P8): shared by the result database, the orchestrator and the summary
class RunLease(BaseModel):           # who is driving the run (§13)
    owner: str                       # process token: pid@host:random
    heartbeat: float                 # last heartbeat (epoch seconds)

class LLMUsage(BaseModel):           # llm_calls totals of one run
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None           # empty when some successful call has no price

# API responses (§15.1, added in P8)
class RunProgress(BaseModel):        # read straight from batch_results
    batches_total: int
    pending: int
    running: int
    completed: int
    failed: int

class RunSummary(BaseModel):         # shared by GET /runs/{id} and the worksheet's second sheet
    rows: int
    bands: dict[Band, int]           # green / yellow / red
    reviews: dict[ReviewStatus, int] # accepted / modified / rejected
    review_pending: int              # red rows not yet reviewed in a review run
    coverage: dict[Literal["oe", "msrp", "retail", "should_cost"], float]   # by system results (manual prices excluded)
    rows_with_failure: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    llm_cost_usd: float | None       # empty when some model has no price
    elapsed_s: float | None          # finished_at − created_at (wall clock, including downtime after a kill)

class RunDetail(BaseModel):
    run: RunRecord
    progress: RunProgress
    summary: RunSummary | None       # empty before finalize
    lease_active: bool               # some process is driving the run (lease not expired)

class RowPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[WorksheetRow]

class RowDetail(BaseModel):
    row: ResolvedRow
    reviews: list[ReviewEntry]
    in_review_queue: bool

class ReviewOutcome(BaseModel):
    run_id: str
    row_id: int
    done: bool                       # False: accepted, processing in the background (202)
    row: ResolvedRow | None          # the reviewed row when done

# Evaluation (§14, added in P10); response of `GET /runs/{id}/eval`
class OEEval(BaseModel):
    coverage: float                  # resolved / all rows
    accuracy: float | None           # share of resolved rows whose current_oe equals the primary OE
    conclusion_accuracy: float       # OE conclusion accuracy over all rows (correct not_found / multiple included)
    chain_accuracy: float | None     # among rows resolved by both system and truth, share with an identical supersession chain

class MSRPEval(BaseModel):
    coverage: float                  # found or discontinued / all rows
    accuracy: float | None           # among rows with a price, share with relative error ≤1%
    status_accuracy: float
    mean_rel_error: float | None

class SiteEval(BaseModel):
    expected: int                    # rows whose truth OE is resolved and that are listed at this site (other rows are, by rule, not looked up for retail prices)
    quoted: int                      # rows with a quote from this site
    found: int                       # rows in expected that got a quote
    coverage: float | None           # found / expected

class RetailEval(BaseModel):
    sites: dict[str, SiteEval]
    selected_accuracy: float         # reference retail site correct (selecting none when truth expects none also counts)
    outlier_precision: float | None  # over (row, site) pairs, only rows whose truth is resolved
    outlier_recall: float | None

class ShouldCostEval(BaseModel):
    spec_accuracy: float             # category, material, weight (≤1%)
    landed_mape_part: float | None   # rows using the part's own cycle time
    landed_mape_default: float | None   # rows using the category default cycle time
    n_part: int
    n_default: int

class BandEval(BaseModel):
    rows: int
    usable: int                      # rows usable as is
    row_correct: int

class ReliabilityBin(BaseModel):     # interval [lo, hi); the last one includes 100
    lo: int
    hi: int
    rows: int
    mean_score: float | None         # mean of score / 100
    usable_rate: float | None

class CurvePoint(BaseModel):         # only rows scoring ≥ threshold are handled automatically
    threshold: int
    coverage: float
    accuracy: float | None           # by "usable as is"

class ThresholdPick(BaseModel):      # lowest threshold reaching the target accuracy
    target: float
    threshold: int | None
    coverage: float | None

class CalibrationEval(BaseModel):
    row_correct_rate: float
    usable_rate: float
    ece: float
    auroc: float | None              # empty when there is only one label class
    bands: dict[Band, BandEval]
    red_row_correct_rate: float | None   # share of red rows that are row correct (rows correctly held back)
    reliability: list[ReliabilityBin]
    curve: list[CurvePoint]
    thresholds: list[ThresholdPick]  # targets 0.90 / 0.95 / 0.99

class AgentUsage(BaseModel):         # llm_calls grouped by agent
    agent: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None           # empty when some successful call has no price

class UsageEval(BaseModel):
    agents: list[AgentUsage]
    models: dict[str, str]           # role → provider/model (from llm_calls)
    cost_usd: float | None           # calls of this run
    source_cost_usd: float | None    # copied runs: calls of every source run up the copy chain (agent cost); otherwise empty
    total_cost_usd: float | None     # sum of the two
    elapsed_s: float | None
    cost_per_1k_rows: float | None   # total_cost_usd / rows × 1000

class RowEval(BaseModel):
    row_id: int
    score: int | None
    band: Band | None
    row_correct: bool
    usable: bool                     # usable as is

class InjectionEval(BaseModel):     # error injection experiment (§14); InjectionKind = "oe" | "msrp" | "retail"
    rows: int                        # number of injected rows
    bands_by_kind: dict[InjectionKind, dict[Band, int]]
    held_back_rate: float | None     # share of injected rows in the yellow or red band
    green_injected: int              # injected rows in the green band (wrong rows that would be used directly)
    auroc: float | None              # non-injected usable rows vs injected rows

class RunEval(BaseModel):            # schema_version "1.1" (added injection)
    run_id: str
    created_at: str
    strategy: StrategyName
    truth_path: str
    rows: int
    reviewed_rows: int               # rows evaluated on the system conclusion before review
    oe: OEEval
    msrp: MSRPEval
    retail: RetailEval
    should_cost: ShouldCostEval
    calibration: CalibrationEval
    usage: UsageEval
    row_evals: list[RowEval]
    injection: InjectionEval | None = None   # only for runs of the injection experiment
```

The result database's internal persistence models (the four-path results per row, `RowOutcomes`, and the finalized row, `StoredRow`) are used only between the orchestrator and the result database, are defined in `store/runs.py`, and are not cross-module contracts. Exception: the error injection experiment (§14) rewrites the four-path results in the result database directly, so `eval/inject.py` also uses `RowOutcomes`.

LLM output must be parsed into the models above with structured output; on a validation failure the error is appended to the prompt and retried, at most twice; if it still fails, the row outputs a `RowFailure` with the reason, and **the batch must not crash**.

---

## 8. LLM layer

- `llm/factory.py`: `get_chat_model(role: str)` returns a model according to `settings.yaml`. `role` distinguishes the agents and the judge, so the judge can use a different model.
- Supported providers: `deepseek` (default), `anthropic`, `qwen` (OpenAI-compatible API), `fake`.
- `llm/fake.py`: **FakeLLM**. It registers rule-based handlers per task type (for example, extracting OE numbers and prices from page text with regular expressions) and returns valid structured output. Its purpose is to let the whole pipeline run at zero cost in tests; **accuracy is not required**. The FakeLLM must not read the ground truth.
- `llm/tracking.py`: every call writes `run_id, agent, model, input tokens, output tokens, cost, latency` to the `llm_calls` table. Unit prices live in the config.
- Concurrency: a global asyncio Semaphore, at most 8 concurrent LLM calls by default.
- With the environment variable `RFQ_LLM_PROVIDER=fake`, the FakeLLM is forced globally.

---

## 9. Mock external tools (`tools/`)

Simulate the web lookups of the original system. Every tool implements one interface, so a real implementation can replace it later:

```python
class ToolProvider(Protocol):
    name: str
    async def search(self, query: str) -> list[ToolHit]   # ToolHit: page_id, title, text
```

| Tool | Description |
|---|---|
| `oem_catalog_search` | Look up OEM catalog pages by part number |
| `interchange_search` | Look up interchange candidates by customer part number |
| `msrp_lookup` | Look up the maker's price page by OE number |
| `retail_search_{1..5}` | One per retail site, 5 in total |
| `external_spec_search` | Look up external product pages for material and weight |

The tools return **noisy text**; the LLM extracts and judges, which is what gives the LLM real work to do. The tools support fault injection (timeouts, empty results), switched on in the config, for testing fault tolerance.

---

## 10. MCP server (internal database)

`mcp_server/server.py`: implemented with the official Python MCP SDK, stdio transport, backed by SQLite. It provides these tools:

| MCP tool | Description |
|---|---|
| `lookup_our_part` | Customer part number → our part number |
| `get_part_spec` | Our part number → material, weight, category, cycle time |
| `get_material_price` | Material → current $/kg |
| `list_materials` | Every material key known to the internal database (added in P5, for mapping external material wording) |
| `get_cost_params` | Returns the rates in cost_model.yaml |

Agents connect to this server through `langchain-mcp-adapters`. There is also an `rfq mcp serve` command to start it on its own for debugging with MCP Inspector.

---

## 11. The four subagents

Each agent is an independent LangGraph subgraph or callable with its own CLI subcommand, for example `rfq agent oe-interchange --input data/samples/rfq_sample_20.xlsx`, which writes JSONL.

### 11.1 `oe_interchange`

- **Input**: `RFQLine`
- **Flow**:
  1. If `our_part_no` is given, or can be found through MCP `lookup_our_part` → record it as a known match.
  2. Call `interchange_search` and `oem_catalog_search` for candidates.
  3. The LLM extracts candidate OE numbers and supersession relations (superseded by / replaces) from the page text.
  4. **Deterministic code** builds the supersession chain from those relations and takes the end of the chain as `current_oe`; a cycle raises an error and is flagged.
  5. With several candidates and no way to decide, `status="multiple"` and all candidates are kept.
- **Output**: `OEResolution`
- **Tests**: supersession chain building (including cycle detection), part-number normalization, one-to-many cases.

### 11.2 `oe_msrp`

- **Input**: `OEResolution.current_oe`
- **Flow**: `msrp_lookup` → the LLM extracts the MSRP from the page and judges whether the part is discontinued → output. If the current OE number has no price, **walk back along the supersession chain** to try older numbers, and say so in the reasoning.
- **Output**: `MSRPResult`
- **Business rules** (confirmed in P3):
  - Rows whose OE resolution is `multiple` are not looked up: `status=not_found`, `oe_part_no` empty, and the reasoning says "OE not uniquely resolved; pending manual selection". The system does not guess the primary OE.
  - Rows whose OE resolution is `not_found` output `not_found` directly; rows whose OE resolution failed also fail for MSRP (marked as an upstream failure).
  - When the price comes from an older number by fallback: `oe_part_no` stays the current OE number, `quote.matched_part_no` records the older number that actually gave the price, and the reasoning states the fallback; discontinued or not is judged only from the page that gave the selected price.
  - A price is used only when the number really appears as an amount in the page text (same rule as "no fabrication" in §11.3); an MSRP ≤ 0 is not used and counts as no price for that number, so fallback along the chain continues.
  - When the same number has several different usable prices, take the higher one, mark "price conflict" in the reasoning, and keep the conflicting price pages as evidence. Pages that fail grounding are not evidence; they are recorded only in the trace.
  - Only US-dollar prices are used: a page with no currency counts as USD; a price explicitly in another currency is not used (counts as no price for that number, so fallback continues), and the reasoning says so. No currency conversion.

### 11.3 `retail_price`

- **Input**: `current_oe` and every number on the supersession chain
- **Flow**:
  1. Query the 5 retail sites concurrently.
  2. Each site may return several results; the LLM picks the **most precise match** and gives its reasoning and `match_quality`.
  3. Extract the list price and sale price, and compute the discount rate and `effective_price`.
  4. **Deterministic code** does outlier detection: a price more than the configured threshold (default ±40%) away from the median is marked as an outlier.
  5. Rule for `selected`: among non-outlier results, prefer an exact match, then the lowest `effective_price` that is in stock. The rule lives in its own function so it can be adjusted later.
- **Output**: `RetailResult`
- **Business rules** (confirmed in P4):
  - Upstream handling as in §11.2: rows whose OE is `multiple` are not looked up for retail prices (the reasoning says "OE not uniquely resolved; pending manual selection"); rows whose OE is `not_found` output an empty result directly; rows whose OE resolution failed are recorded as failures (marked as an upstream failure).
  - Division of work: the LLM only extracts facts from the page (the part number as written in the title, list price, sale price, currency, in stock or not, price snippet); the "most precise match" judgment is made by deterministic code comparing part numbers.
  - `match_quality`: `exact` when the title part number is written exactly like `current_oe`; `normalized` when equal after normalization; `fuzzy` when listed under an older number on the supersession chain (the reasoning names the older number). Results that, after normalization, equal no number on the chain are always discarded.
  - When a site has several matches, compare in turn: match_quality (exact > normalized > fuzzy) → in stock first → lowest `effective_price` → page_id; the reasoning states the number of matches and the chosen page, and the losing pages go to the trace.
  - No fabrication: both the list price and the sale price must appear **as amounts** in the page text, otherwise the whole page is not used (recorded in the trace). An amount means a number with a currency marker (e.g. `$25.75`, `25.75 USD`, `USD 25.75`, `EUR 60.00`), or a standalone number in a price line (containing words such as price / MSRP / list / sale / was / now / each); digits inside part numbers and numbers with units (e.g. `12,000 miles`) do not count. Prices must be greater than 0; a page with a price ≤ 0 is not used. Only US-dollar prices are used; a page with no currency counts as USD; other currencies are not used; no currency conversion.
  - An extraction whose page_id is not among the search results, or whose title part number does not appear in the page text, is not used as a whole page (no fabrication, recorded in the trace).
  - The discount is computed by code: `discount_pct = (list price − sale price) / list price × 100`, rounded (ROUND_HALF_UP) to 1 decimal place; a sale price ≥ the list price is ignored; with only a sale price, the list price and discount rate are empty and `effective_price` is the sale price.
  - Stock: "Ships today" on the page counts as in stock. When the page states no stock status, `in_stock` is empty (not changed to false); price selection treats it as out of stock and the reasoning says so.
  - Outlier detection: compare `effective_price`; the median is taken over all quotes of the row (including out-of-stock and fuzzy ones); `|price − median| / median > retail_outlier_threshold` is an outlier; with fewer than 3 quotes no detection is done and the reasoning says so.
  - `selected`: exclude outliers → tier by match_quality (exact > normalized > fuzzy) and take the highest non-empty tier → in-stock first (no restriction if all are out of stock) → lowest `effective_price` → site name. When every quote is judged an outlier nothing is selected (`selected` is empty), and the reasoning says "every price deviates from the median; no reliable reference price; needs manual confirmation".
  - Site search failure: if any number at a site still fails after retries, the whole site counts as failed, gives no quote, and the reasoning says so; if all 5 sites fail, the whole row is recorded as a failure.

### 11.4 `should_cost`

- **Input**: `RFQLine`, `OEResolution`
- **Flow**:
  1. Get the spec (material, weight) from the internal database through MCP.
  2. When the internal database lacks it, call `external_spec_search` and let the LLM extract it from the page.
  3. When both sides have values, compare them and record the result in `spec_source`. **On disagreement the internal database wins**; external pages only fill fields missing internally.
  4. **All computation is done by deterministic code** (the LLM does no arithmetic), with these formulas; every rate is read from `cost_model.yaml`:

```
material      = weight_kg × material_price_per_kg × (1 + scrap_rate)
conversion    = cycle_time_min / 60 × (labor_rate_per_hr + machine_rate_per_hr)
overhead      = conversion × overhead_rate
sga           = (material + conversion + overhead) × sga_rate
profit        = (material + conversion + overhead + sga) × profit_rate
packaging     = fixed value per category
FOB           = material + conversion + overhead + sga + profit + packaging

freight       = ocean freight per unit by category
insurance     = (FOB + freight) × insurance_rate
duty          = FOB × (mfn_duty_rate + additional_tariff_rate)   # US duty is assessed on the FOB value
mpf           = FOB × mpf_rate
brokerage     = brokerage_per_entry / units_per_shipment
inland_freight= inland freight per unit by category
landed_cost   = FOB + freight + insurance + duty + mpf + brokerage + inland_freight
```

- The top of `cost_model.yaml` must carry the comment: **all rates are illustrative and do not represent any real tariff or quote.**
- **Output**: `ShouldCostResult` (with a complete `CostBreakdown`)
- **Business rules** (confirmed in P5):
  - Our part number comes first; the OE status only affects external lookups: when our part number is known (given in the RFQ or found in the cross-reference) → get the internal spec (category, material, weight, cycle time) through MCP, regardless of the OE status. External pages are looked up by `current_oe` only when OE is `resolved` (older chain numbers are not looked up); for `multiple` the system does not pick a primary OE, and `not_found` has no number to look up, so external counts as absent and the reasoning says why. Rows whose OE resolution failed are recorded as failures (marked as an upstream failure).
  - When our part number is unknown (as in the original system): estimate from external pages only. The category comes from the OEM catalog page found by `current_oe` (the LLM extracts the category text, code maps it: containing `thermostat` → thermostat, containing `pump` → water_pump); if the catalog page has none, use the RFQ description (same mapping); still uncertain → `insufficient_data`. Cycle time uses the category default from `cost_model.yaml`, noted in the reasoning. When an our part number given in the RFQ or found in the cross-reference does not exist in the internal database, handle it the same as "our part number unknown", with the reasoning saying the number is not in the internal database. When the internal record exists but has no category, fall back for the category in the same order (OEM catalog page → RFQ description), use internal values for the other fields, and say "internal database lacks the category" in the reasoning; in that case the catalog page is used to fill data, and a lookup failure is a row failure.
  - Whenever OE is `resolved`, look up external spec pages: to cross-check when the internal data is complete, to fill gaps when it is not. At most 1 LLM call per row (external spec pages and, when needed, catalog pages are extracted together).
  - Field by field (material, weight): both sides have values that agree → `both_agree`; both sides disagree → `conflict` (internal value used); internal only → `internal`; external only → `external` (filled from external); neither side → `missing`. Whole-row `spec_source`: if any field is `missing` → `missing` and `status=insufficient_data`; otherwise the weaker of the two fields, ordered `both_agree > internal > external > conflict`.
  - Agreement means: weight, converted to kg, within relative error `pipeline.spec_weight_tolerance` (default 0.05); same material key.
  - Material mapping: the LLM extracts the material text from the page and picks one of the internal database's known material keys (MCP `list_materials`), or `other` if none fits; code checks that the text appears in the page.
  - No fabrication (a field that fails counts as externally absent and is recorded in the trace): the page_id must be among the search results and the text must mention `current_oe`; the weight's "number + unit" must appear together in the text, accepting only kg / g / lb (including kgs, lbs, case-insensitive), > 0 after conversion; the material text and category text must appear in the page (case-insensitive). Weight is rounded (ROUND_HALF_UP) to 3 decimal places right after conversion to kg (gram precision, same as the internal database), and the rounded value is used for agreement checks, cost computation and output; amounts in `breakdown` are not rounded.
  - Several grounded external pages for the same `current_oe`: if they agree (weight within tolerance, same material key) they merge into one external value (the value of the page with the smallest page_id, with all pages kept as evidence); if they disagree, that field's external value is unusable—with an internal value the field is `conflict` and uses the internal value; without one the field is `missing` and the reasoning says "external pages disagree; needs manual confirmation". Catalog pages that disagree on the category → category unknown.
  - Other computation inputs: material `other`, `get_material_price` not found or not USD, or unknown category → `insufficient_data`, empty `breakdown`, and the reasoning states what is missing; `spec_source` is still given by the rules above. A part without a cycle time uses the category default.
  - `material` / `weight_kg` hold the values actually used for computation; with `insufficient_data`, known fields are still filled and missing ones are empty.
  - Failures: when external pages are only for cross-checking, a search or extraction failure is not a row failure; the estimate uses internal values (`spec_source=internal`) and the reasoning notes the failed cross-check. When external pages are needed to fill data, a search or extraction failure is a row failure (`search_spec` / `extract_spec`), distinct from `insufficient_data`, where the data really does not exist; an MCP internal-database call error is a row failure (`internal_spec`).
- **Tests**: unit tests for each formula term; `status="insufficient_data"` when the spec is missing.

---

## 12. Entity resolution, confidence and output

### 12.1 Entity resolution (`resolution/`, fully deterministic)

- Handles three kinds of part number: customer part number, OE number, and listed part number (the part number as written in a retail product title, i.e. how the OE number is written on a retail site; formerly called "retail SKU", though the sites have no stock numbers of their own). They are separate fields in the schema and **must not be mixed**.
- Merge the four results of `oe_interchange`, `oe_msrp`, `retail_price` and `should_cost` into one `WorksheetRow` per `row_id`.
- For many-to-one (several customer part numbers mapping to the same OE number), price results are reused and the sharing is noted in the row.
- The output of resolution is one `ResolvedRow` per row (containing the flat `WorksheetRow` and the four complete results).
- **Business rules** (confirmed in P6):
  - Many-to-one: rows whose OE is `resolved` and whose `current_oe` is equal after normalization form a "shared-OE group". Within the group, MSRP and retail results all come from the representative row; the representative is the group's smallest row_id without a row failure on that path (chosen separately for the two paths). Each row notes "shares the OE with rows X, Y; prices taken from row X". When the rows' own price conclusions differ (MSRP status or value, selected site or price), the representative's result is still used, and the review reason says "rows sharing the OE reached different price conclusions". Should-cost is not shared (it depends on our part number). Rows whose OE is `multiple` are not grouped.
  - One-to-many: rows whose OE is `multiple` have an empty `current_oe`; `oe_candidates` lists each candidate's current number (only candidates grounded in the source text; `llm_inferred` ones are not listed and are kept only in `ResolvedRow.oe`); the price columns are empty and the review reason says "OE not uniquely resolved; pending manual selection".
  - When OE is not `resolved` (multiple / not_found), the price agents by rule do not look up prices; the worksheet's price columns and `msrp_status` are empty, and `ResolvedRow` keeps the agents' raw results.
  - Row failures: when a path is a `RowFailure`, the row is still output, that path's columns are empty, `failures` records "path:stage:reason", and the review reason says "a path could not be looked up", worded separately from "looked up but not found" (not_found / insufficient_data). When OE fails, the `upstream_oe` failures of the three downstream paths are reported once as the root cause "OE resolution failed".
  - The row_id sets of the four results and of the RFQ must be identical; otherwise raise an error and output no partial results.
  - Reasoning summary per stage: the first 200 characters of each path; the failure reason when that path is a row failure.
- `WorksheetRow` columns: row_id, customer part number (raw / normalized), our part number, current_oe, supersession chain, OE status, MSRP, effective_price of each retail site, selected retail price and its source, retail outlier flags, should-cost FOB, landed cost, price reference (MSRP, retail price and landed cost shown side by side; the system does not give a final quote), confidence score, band, review reasons, reasoning summary per stage.

### 12.2 Confidence (`confidence/`)

Built as **switchable strategies**, chosen by `confidence.strategy` in `settings.yaml` (default `evidence_plus_judge`, confirmed after the P10 error injection experiment: only it pushes many plausible-looking wrong rows out of the green band; about $1.65 more per 1,000 rows):

| Strategy | Description |
|---|---|
| `evidence` | Weighted deterministic evidence signals only: OE match type, whether the supersession chain is complete, number of price sources found, retail price dispersion, whether MSRP and retail prices are plausible (retail usually should not be far above MSRP), spec source (both_agree > internal > external > conflict). Weights live in the config. |
| `evidence_plus_self` | On top of `evidence`, the LLM gives a 0–100 self-assessment of the key judgments (OE match, retail price selection, MSRP), **sampled twice** (temperature 0.7); the agreement of the two answers and the self-assessed scores form an LLM signal, which is weighted with the evidence signals. |
| `evidence_plus_judge` | On top of `evidence`, rows whose capped evidence score falls in `judge_band` (default 50–100, i.e. capped evidence score ≥ 50) are reviewed by a separate judge model, which sees the evidence and conclusions and outputs agree / disagree and a 0–100 score; the judge can only lower scores (on disagreement it pushes the row to red) and adds nothing when it agrees. |

- Bands: green ≥80 (use directly), yellow 50–79 (spot check), red <50 (must be reviewed by a person). Thresholds live in the config; **final values are decided by the §14 evaluation**.
- Every row outputs `signals` and `explanation`; the dashboard must be able to show "why this score".
- **Business rules** (confirmed in P6):
  - Meaning of the score: one score per row, the confidence that the row's conclusions as a whole can be used directly. Definition of "row correct" (the §14 row accuracy uses it): OE correct (resolved and equal to the primary OE, or multiple with candidates covering the acceptable set, or not_found when not_found is expected); MSRP status correct and, with a price, relative error ≤1%; selected retail site correct; should-cost status and spec (category, material, weight) correct. Any stage failure or any wrong item makes the whole row wrong.
  - "Usable as is" (confirmed after P9; §14 confidence calibration and the coverage-accuracy curve use it, consistent with the meaning of the score): row correct, and OE is `resolved`, and retail is not "has quotes but all are outliers". So rows whose OE is `not_found` / `multiple` are not usable as is even when the conclusion is correct; rows whose should-cost is `insufficient_data` can still count (MSRP and reference retail price are usable).
  - The six `evidence` signals (0–1):
    - `oe_match`: best match type among candidates, exact 1.0, normalized 0.9, interchange 0.7, llm_inferred 0.2.
    - `chain`: 0.3 when the supersession chain is truncated (`chain_truncated`), otherwise 1.0.
    - `price_sources`: n = 1 if MSRP is found / discontinued, plus the number of non-outlier retail quotes; signal = min(n, 4) / 4.
    - `retail_dispersion`: coefficient of variation cv (population standard deviation / mean) of the non-outlier quotes' `effective_price`; signal = max(0, 1 − cv / `retail_outlier_threshold`); 0.5 with fewer than 2 quotes.
    - `msrp_retail`: r = reference retail price / MSRP; r ≤ 1.0 scores 1.0, r ≤ `msrp_retail_suspicious_ratio` (default 1.2) scores 0.5, above that 0; 0.5 if either is missing.
    - `spec_source`: both_agree 1.0, internal 0.8, external 0.5, conflict 0.3, missing 0.
    - Evidence score = ROUND_HALF_UP(100 × Σ(weight × signal) / Σweight). Weights and per-level values live in the config (§18).
  - Caps (gates; apply to all three strategies and run last; the LLM cannot lift a capped row):
    - Capped at the red ceiling (`yellow_min − 1`): OE is `multiple`; OE is `not_found`; any path has a row failure; retail has quotes but all are outliers (no quote at all is not capped; the signals lower the score).
    - Capped at the yellow ceiling (`green_min − 1`): rows sharing the OE reached different price conclusions; should-cost is `insufficient_data`.
    - Triggered caps are written into `signals` (`gate_*`) and the explanation.
  - Blended scores always use the **capped evidence score** (the same basis as choosing judge rows), and the cap is applied again after blending.
  - `evidence_plus_self`:
    - Called only for rows without a red cap (rows whose evidence score is below 50 without a red cap are called too). Role `self_eval`, same model as the main one, temperature 0.7.
    - The same prompt is sampled `self_samples` times. The LLM answers the key judgments itself and gives its confidence (0–100): the current OE number it believes correct; with a reference retail price, the site it would choose; with an MSRP quote, the MSRP it reads from the MSRP evidence snippets (a US-dollar number, added in P10; the prompt states the §11.2 rules: US dollars only, take the higher price for the same number, prices of older chain numbers may be used). The MSRP section of the self-assessment brief gives only the evidence (source, page id, source snippet) and the number looked up, not the price and status extracted by the system.
    - Signal of each judgment = 0.5 × score + 0.5 × consistency, where: score = the mean over samples of (confidence / 100 when the answer agrees with the system conclusion, otherwise 0); consistency = 1 when all answers are identical (part numbers compared after normalization), otherwise 0. For the MSRP question "agrees" means relative error ≤1%: within 1% of the system MSRP (system MSRP as denominator) agrees with the system; consistency is 1 when every sample gives an answer within 1% of the first answer (first answer as denominator). The correctness check shares one function with the §14 MSRP check.
    - LLM signal = mean of the judgment signals. Final score = ROUND_HALF_UP((1 − `self_weight`) × capped evidence score + `self_weight` × 100 × LLM signal), then capped.
    - When some samples fail, only successful samples are used; when all fail, the evidence score is used and the explanation says so. Neither counts as a row failure.
  - `evidence_plus_judge`:
    - The `judge` role is called only for rows whose (capped) evidence score falls in `judge_band`, once per row.
    - The judge sees the row's whole evidence and conclusions (without ground truth) and outputs agree, score (0–100) and concerns.
    - The judge prompt states the business rules the system applies on purpose (confirmed after P9): on internal vs external spec conflicts the internal database wins; for MSRP, take the higher of several prices for the same number, fallback to older chain numbers is allowed, US dollars only; a retail outlier deviates from the median by more than `retail_outlier_threshold`, and the reference retail price is chosen by match quality → in stock → lowest effective price; rows whose OE is `multiple` are not looked up for prices. It also says these rules are not concerns: the judge only checks whether the evidence was read correctly and the rules applied correctly. The rule text exists only once in the code.
    - When the judge agrees, final score = capped evidence score (no increase; confirmed after the P10 injection experiment: raising on agreement lifted wrong rows the judge missed into the green band), and its concerns (if any) also go into the review reasons as spot-check hints (confirmed after P9). When it disagrees, blended score = ROUND_HALF_UP((1 − `judge_weight`) × capped evidence score + `judge_weight` × score), then capped at red, and never higher than the capped evidence score (the judge can only lower scores, under any `judge_band` setting); the concerns go into the review reasons.
    - When the judge call fails, the evidence score is used and the explanation says so. Not a row failure.
    - How the judge appears in the review reasons (P10): on disagreement `judge: <concerns>` ("disagrees with the conclusions" when concerns are empty); on agreement with non-empty concerns `judge note: <concerns>`, nothing when empty.
  - Review reasons: filled for every band, including caps, path failures, MSRP not found / discontinued / fallback to an older number, no reference retail price, outlier sites, spec conflict / missing / insufficient data, shared-OE price disagreement, and the judge's concerns (on both agreement and disagreement). The explanation spells out how the score was built: the evidence score before capping and the caps triggered, the weakest signals, the LLM part (stating the capped score that went into the blend), and whether the blended result was capped again.

### 12.3 Output

- Excel pricing worksheet: one row per RFQ part number, the whole row colored by band (green / yellow / red), frozen header; the second sheet is the run summary (rows, count per band, coverage, LLM cost, elapsed time).
- Also written to the SQLite result tables for the API to read.
- **Details** (confirmed in P8):
  - The worksheet is generated from the result table `run_rows`: the API generates it fresh on every download (reflecting the latest review); the CLI writes `run_<run_id>.xlsx` after finalizing.
  - Columns use English field names (same as the API), in this order: row_id, customer part number (raw / normalized), our part number, OE status, current_oe, supersession chain, OE candidates; the three price reference columns MSRP, reference retail price and landed cost side by side; then MSRP status, effective price per site (one column per site), reference retail price source, outlier sites, should-cost FOB, shared-OE rows, price source row, shared prices disagree, confidence score, band, review status, review reasons, row failures, and one column per path for the reasoning summary. Lists are joined with commas (the supersession chain with ` -> `, review reasons and row failures with line breaks).
  - Colors: green `C6EFCE`, yellow `FFEB9C`, red `FFC7CE`, filling the whole row; review does not change the color (the band stays; the review status shows in its column). Freeze the header and the row_id column, and add an auto-filter to the header.
  - Summary (`RunSummary`, same as `GET /runs/{id}`): coverage is computed on system results—OE resolved, MSRP found or discontinued, has a reference retail price, should-cost estimated, each as a share of all rows, excluding manual prices; elapsed time is the wall-clock `finished_at − created_at` (including downtime after a kill).

---

## 13. Orchestrator (LangGraph main graphs)

- **Run**: one complete `rfq run` over one RFQ, with a unique `run_id`. Intake happens once when the run is created and the RFQ lines are written to the result database; resume / rerun never read the Excel again. Run parameters (dataset, batch size, confidence strategy, review or not) are stored in the `runs` table and reused by resume / rerun.
- **Two kinds of graph** (both with LangGraph's SQLite checkpointer; checkpoints are a separate file, `data/checkpoints.db` by default):
  - **Batch graph**, `thread_id = f"{run_id}:{batch_id}"`. State: `run_id`, `batch_id`, the batch's `RFQLine` list, a result dict per stage (indexed by row_id), an error list. Nodes: `load_batch → oe_interchange (Send per row) → plan_prices → [oe_msrp ∥ retail_price ∥ should_cost] (Send per row) → collect → persist_batch`.
  - **Finalize graph**, `thread_id = f"{run_id}:final"`, runs over the whole table after all batches: `resolve (entity resolution) → confidence (Send per chunk) → (review) → finish`. Entity resolution and confidence happen at finalize because many-to-one shared-OE groups usually span batches (the §12.1 rules must hold across the whole table).
  - The four agent subgraphs are called as plain functions (they do not inherit the checkpointer); the checkpoint granularity is "one row, one agent".
- **Batching**: the outer loop splits by `batch_size` (default 100) in row_id order, and batches run one after another. Each batch's four-path results are written to the result database in one transaction, and `batch_results` marks it `completed`. A row-level failure inside a batch does not affect other rows; an exception that row-level fault tolerance cannot absorb (e.g. MCP fails to start, a database write error) marks the whole batch `failed` with no results stored, and later batches go on. At finalize, every row of a failed batch has all four paths recorded as row failures (stage `batch`) and still goes into the worksheet.
- **Price reuse for many-to-one** (confirmed in P7): only for rows whose OE is `resolved`, deduplicated by normalized `current_oe`; the same `current_oe` runs `oe_msrp` and `retail_price` only once in the whole run:
  - First check the result database for saved results of that `current_oe`; if that path is not a row failure, reuse it directly (MSRP and retail judged separately); row-failure results are not reused; the old results of the rows being rerun are not reused (a rerun row reruns all four paths); only results a row produced itself are reused, not copies handed to other rows by many-to-one.
  - The first row met in a batch (smallest row_id) is the price leader and runs the price agents with its `OEResolution`; other rows in the batch copy the results (only `row_id` changes); when the leader fails, the group members in the batch record the same failure on that path.
  - Should-cost is not deduplicated (it goes by our part number). Because the group looks up prices only once, "rows sharing the OE reached different price conclusions" never occurs in orchestrated runs.
- **Rescore (P10)**: `rfq rescore RUN_ID --strategy S [--allow-real]` creates a new run that copies the source run's RFQ lines, batches and four-path results (batch status kept as is: completed batches stay `completed`, failed batches stay `failed`, and their rows are recorded as batch-failure row failures at finalize), and only finalizes (resolution + confidence, no review); the four agents are not rerun. `RunSpec` follows the source run (`strategy` replaced by S, `review=False`) and records `rescored_from`. The source run must be finalized and have no human review records. Before running, it prints the exact number of judge / self calls (the source run's evidence scores are known). Used to compare confidence strategies on the same agent results (§14).
- **Error injection (P10 add-on experiment)**: `rfq eval inject SOURCE [--rows 40] [--seed 7]` copies the source run the same way as rescore (same requirements), rewrites the four-path results of injectable rows, and finalizes with `evidence` (no LLM calls); which rows were injected is recorded in the result database table `run_injections`; copying, rewriting and recording happen in one transaction. It can then be `rfq rescore`d, and the injection records are copied along with the run. Rules in §14.
- **Checkpoints and resume**: whether a batch is complete is decided by the result database, and `completed` batches are skipped; an incomplete batch with a checkpoint (the process was killed) resumes with the same `thread_id` and empty input, rerunning only the unfinished row-level tasks; a batch without a checkpoint starts over. `failed` batches are not retried on resume. The finalize graph works the same way, continuing a pending review if there is one.
- **CLI**:
  - `rfq run --input X.xlsx [--review] [--strategy] [--batch-size] [--limit] [--dataset] [--allow-real]`: create a run, return the run_id
  - `rfq run --resume RUN_ID`: skip completed batches and continue from the breakpoint
  - `rfq rerun-failed RUN_ID`: rerun failed batches, plus rows of completed batches with a row failure on any path (the whole row reruns all four paths, new results replace old ones), then redo finalize. It first clears the old checkpoints of those batch threads and the finalize thread (including a pending review from a paused review), then records the rerun plan in the result database (the run goes back to running, each batch to rerun goes back to pending with the rows to rerun recorded), then each batch starts with new input; if killed midway, `--resume` finishes the remaining batches by the recorded plan and redoes finalize from scratch (the review queue is rebuilt from the rerun results, queuing only red rows not yet reviewed; decisions already made are kept)
  - `rfq run --input X.xlsx --review`: enable human review
  - `rfq runs show RUN_ID`: status and attempts of each batch, band distribution, LLM call count and cost
  - Before running, print the provider / model and expected LLM call count per role; real providers require `--allow-real` (so do resume and rerun-failed).
  - After finalize (status `awaiting_review` or `completed`), export `data/runs/run_<run_id>.jsonl` (one `ResolvedRow` per line) and the pricing worksheet `run_<run_id>.xlsx` (§12.3).
- **Human review (`--review`, revised in P8)**: only runs created with review enabled (`RunSpec.review`) can be reviewed. **Finalize completes first and the worksheet is downloadable; review happens on the finalized run**: after finalize the run is `awaiting_review` while red, unreviewed rows remain, and `completed` when the queue is empty; runs with `review=False` go straight to `completed` after finalize. The review node hands over the whole review queue with one `interrupt()` (`ReviewQueue`, by row_id); the resume value is a decision on one row of the queue (`ReviewDecision.row_id`), and decisions on rows not in the queue are ignored. Each decision gets its own checkpoint; after it is recorded, the graph goes back to `resolve` to re-summarize (rows whose fingerprint did not change keep their score), then suspends again with the remaining queue; rows that turn red during review join the queue. The CLI shows the head of the queue each time with its evidence and conclusions, used as before; the API can answer any row in the queue (§15.1). Review records go to the `review_log` table (action, OE / MSRP / reference retail price before and after, a snapshot of the whole row before the change), and the outcome goes to `WorksheetRow.review_status`. Business rules (confirmed in P7):
  - **Accept**: only records the review status `accepted`.
  - **Reject**: the row stays on the worksheet with values and band unchanged; review status `rejected`.
  - **Modify OE number**: build an `OEResolution` from the entered number (`resolved`, a supersession chain with only that number, our part number kept from the original result, candidate `match_type="manual"`, evidence source `manual_review`), and rerun `oe_msrp`, `retail_price` and `should_cost` for that row; after review the whole table is resolved again (shared groups may change) and rows whose results changed get their confidence recomputed. Review status `modified`; the row does not go back into the review queue; a later `rerun-failed` does not rerun OE for it.
  - **Modify price**: only overrides the MSRP and / or reference retail price on the worksheet (USD, > 0): `msrp_status` becomes `found`, `selected_retail_source` becomes `manual_review`, and the review reasons get "price entered manually"; the agents' raw results are unchanged, no agent reruns, no confidence recomputation. Review status `modified`. Manual prices are only overlaid on the worksheet: when the row is rescored later for another reason (e.g. rerun-failed), confidence (including what the judge sees) is still computed from the system's own conclusions, not the manual prices (confirmed after the P7 review).
- **Run lease** (P8): only one process may drive a run at a time. The `runs` table records `lease_owner` (process token) and `lease_heartbeat`; driving starts with one atomic UPDATE (owner empty, owner is itself, or heartbeat older than `storage.lease_ttl_s`, default 60 seconds); while holding the lease, the heartbeat is refreshed every `storage.lease_heartbeat_s` (default 10 seconds), and a refresh that finds the lease taken over by someone else stops driving; the lease is released at the end. When the process holding the lease on the same host no longer exists (killed), it is taken over immediately without waiting for the heartbeat to expire; a lease the API hands to a worker is switched by the worker to its own process identity. Failing to take the lease is a clear error (naming the holder and the seconds since its last heartbeat). Operations that take the lease: `rfq run` (new and `--resume`), `rerun-failed`, workers started by the API, review submissions through the API; read-only queries do not.
- **Background worker** (P8): after creating a run, the API starts a separate subprocess running `rfq run --resume RUN_ID` (detached from the API process, unaffected by API restarts); the API takes the lease first and then hands it to the worker. A killed worker's run can be continued with `POST /runs/{id}/resume` or CLI `--resume` once its lease expires.
- **SQLite with multiple processes** (P8): both the result database and the checkpoint database use `journal_mode=WAL` and set `busy_timeout`.
- **Acceptance**: run 8,000 rows, kill the process by hand midway, and after `--resume` completed batches are not executed again and the final result equals that of a run finished in one go.

---

## 14. Evaluation scripts (`eval/`)

`rfq eval --run RUN_ID` compares a run's results with `ground_truth` and generates `reports/eval_{run_id}.md` and chart PNGs.

| Metric | Description |
|---|---|
| OE resolution | Coverage (share resolved), accuracy (share with a correct current_oe), share with a fully correct supersession chain |
| MSRP | Coverage, error (relative error ≤1% counts as correct) |
| Retail price | Coverage per site, whether selected is correct, precision and recall of outlier detection |
| Should-cost | Spec extraction accuracy; MAPE of the cost estimate against ground truth |
| Confidence calibration | Labeled by "usable as is" (§12.2): actual share per band, ECE, AUROC, reliability diagram; also report the share of "row correct" rows within the red band (rows correctly held back) |
| Coverage-accuracy curve | When only rows scoring ≥ threshold are handled automatically, how coverage and accuracy (by "usable as is") change with the threshold; used to choose thresholds |
| Cost and time | Calls, tokens and cost per agent, total elapsed time, cost per 1,000 rows |

`rfq eval compare --runs A B C`: compares runs with different confidence strategies or models and outputs a comparison table. This is the basis for choosing the confidence strategy and thresholds.

Details (confirmed in P10):
- `rfq eval --run RUN_ID [--truth PATH] [--out reports]`: evaluates only finalized runs (`awaiting_review` / `completed`). The ground truth is inferred: after removing the `<uuid>_` prefix of API upload file names, `rfq_sample_*` uses `<dataset>/<stem>_ground_truth.jsonl`; when the dataset directory has an RFQ with the same name (the dataset's own RFQ, or an uploaded copy of it) it uses `<dataset>/ground_truth.jsonl`; paths are resolved against the project root before comparing; when nothing can be inferred, raise an error suggesting `--truth`. Every row of the run must be in the truth with the same normalized customer part number, otherwise raise an error; truth rows beyond the run (runs with `--limit`) are not evaluated.
- Evaluate the system's own conclusions: reviewed rows use the earliest snapshot in `review_log` (before review); manual prices are not evaluated. Rows of failed batches count as wrong, as row failures.
- The result is `RunEval` (§7), stored in the result database table `run_evals` (rerunning overwrites), and written as `reports/eval_{run_id}.md`, `eval_{run_id}_reliability.png`, `eval_{run_id}_coverage.png` (matplotlib). Evaluation does not call an LLM.
- Metric definitions:
  - OE: coverage = resolved / all rows; accuracy = share of resolved rows whose current_oe equals the primary OE; also report OE conclusion accuracy over all rows; fully correct supersession chain = among rows resolved on both sides, chains (normalized) identical.
  - MSRP: coverage = found or discontinued / all rows; accuracy = share of rows with a price whose relative error is ≤1%; also report status accuracy and mean relative error.
  - Retail: coverage per site = rows with a quote / rows whose truth OE is resolved and that are listed at that site; reference retail site accuracy over all rows; outlier detection precision and recall over (row, site) pairs, only rows whose truth is resolved; precision is empty when no outlier is predicted.
  - Should-cost: spec accuracy; landed-cost MAPE only over rows estimated on both sides, reported separately for the part's own / the category default cycle time.
  - Confidence calibration: probability = score / 100, label "usable as is". ECE uses 10 equal-width bins ([0,10) … [90,100]), Σ(rows in bin / total rows) × |mean score − actual share|. AUROC by the Mann-Whitney rank sum (ties get the average rank); empty with only one label class.
  - Coverage-accuracy curve: every integer threshold 0–100; also list the lowest threshold reaching target accuracy 90% / 95% / 99% and its coverage; the curve chart (report PNG and dashboard) draws a vertical line at the lowest threshold for 95%. Evaluation only provides the basis; adjusting band thresholds and weights is decided by the user and then changed in the config.
  - Cost and time: `llm_calls` grouped by agent; cost per 1,000 rows = total cost / rows × 1000; the total of a copied run (rescore, injection) includes the calls of every source run up the copy chain (adding along `rescored_from` all the way up).
- `rfq eval compare --runs A B C [--out reports]`: reads stored evaluations only, and raises an error when one is missing; the runs must use the same truth file and evaluate the same set of rows (the same RFQ), otherwise raise an error. Outputs `reports/compare_<time>.md`, overlaid reliability diagram and coverage-accuracy curve PNGs, and prints the comparison table in the terminal. Columns: run, strategy, models, rows, green / yellow / red, row correct rate, usable-as-is rate, ECE, AUROC, share usable as is in green, share row correct in red, threshold and coverage at target accuracy 95%, LLM calls and cost of this run, total cost, cost per 1,000 rows, elapsed time.
- Comparing the three strategies: first a full run with `evidence`, then `rfq rescore` creates runs with the other two strategies on the same agent results (§13).
- Error injection experiment (P10 add-on, for when every stage is correct on synthetic data and the strategies cannot be told apart):
  - Injectable rows (system results only): no red cap, OE resolved, no row failure, not in a shared-OE group, has an MSRP quote, has a reference retail price and at least one other non-outlier quote.
  - Shuffle the injectable rows by seed and take the first N (error when there are not enough); row i's type cycles through OE / MSRP / retail: OE error = `current_oe` and the end of the supersession chain replaced by an adjacent fake number (last digit +1, 9 becomes 0); MSRP error = MSRP quote × 1.15–1.35; retail error = reference retail price switched to another non-outlier site. Candidates, evidence snippets and other paths are unchanged; the ground truth is unchanged, so injected rows are judged wrong by §12.2.
  - The evaluation also reports `RunEval.injection`: rows per band by type, the share held back (in the yellow or red band), injected rows in green, AUROC (positive class = non-injected rows usable as is, negative class = injected rows). Shown in the report, in `eval compare` (three new columns, `-` for runs without injection) and on the dashboard evaluation page.

---

## 15. API and Dashboard

### 15.1 FastAPI (`api/`)

| Method | Path | Description |
|---|---|---|
| POST | `/runs` | Upload Excel, start a run in the background, return the run_id |
| GET | `/runs` | List runs |
| GET | `/runs/{run_id}` | Run status, progress (completed batches / total batches), summary metrics |
| GET | `/runs/{run_id}/rows` | Row list, filterable by `band` and `status`, paginated |
| GET | `/runs/{run_id}/rows/{row_id}` | Full detail of one row (all evidence and reasoning) |
| POST | `/runs/{run_id}/rows/{row_id}/review` | Submit a human review decision |
| GET | `/runs/{run_id}/worksheet.xlsx` | Download the pricing worksheet |
| GET | `/runs/{run_id}/eval` | Evaluation result (if any) |
| POST | `/runs/{run_id}/resume` | (Added in P8) Continue an interrupted run: start a worker when the lease is free or expired |

All responses use the §7 Pydantic models, with OpenAPI docs generated automatically. The future React front end will depend only on this API.

Details (confirmed in P8):
- Start: `rfq api serve [--host] [--port] [--allow-real]`. Without `--allow-real` while the config uses a real provider, requests that would trigger LLM calls (creating a run, resuming, changing the OE number in review) return 409 with the expected number of calls.
- `POST /runs`: multipart, an `.xlsx` file plus optional `strategy`, `batch_size`, `review`, `limit`, `dataset`; server-side intake (422 on failure); after creating the run it starts a worker (§13) and returns 202 with the `RunRecord`.
- `GET /runs`: `list[RunRecord]`, newest first. `GET /runs/{id}`: `RunDetail` (progress read from `batch_results`).
- `GET /runs/{id}/rows`: `RowPage`; `band` and `status` (review status `unreviewed / accepted / modified / rejected`) can be repeated; `offset` defaults to 0, `limit` defaults to 100 with a maximum of 1000, ordered by row_id. `GET .../rows/{row_id}`: `RowDetail`.
- `POST .../review`: request body `ReviewInput`. Returns 409 when the run did not enable review or is not `awaiting_review`, when the row is not in the current review queue, or when another process is driving the run. Accept / reject / modify price complete synchronously and return 200 with `ReviewOutcome` (`done=true`); modify OE reruns the three downstream paths and returns 202 (`done=false`), finished by a background worker. When the worker handling a modify-OE decision is killed midway, the run is still `awaiting_review` and the row still in the queue: submitting the same decision for that row again completes it (`POST /resume` is only for runs in status `running`).
- `GET .../worksheet.xlsx`: 409 when the run is not finalized.
- `GET .../eval`: returns the stored `RunEval` (§14, produced by `rfq eval`); 404 when there is no evaluation yet. The API does not trigger evaluations.
- Unknown runs or rows return 404.

### 15.2 Streamlit Dashboard (`dashboard/app.py`)

Talks to FastAPI over HTTP only and does not import any `rfq_agent` module (schemas included), which a test checks. Pages:

1. **Upload and run**: upload an RFQ and show a progress bar.
2. **Run overview**: total rows, green / yellow / red counts, coverage per stage, LLM cost, elapsed time.
3. **Worksheet**: a filterable table colored by band.
4. **Row detail**: supersession chain visualization, price comparison across sites (marking outliers and the selected one), cost waterfall (material → … → landed cost), confidence signal breakdown, the LLM reasoning of each stage.
5. **Review queue**: list of red rows, which can be accepted, modified or rejected on the page.
6. **Evaluation**: shows the §14 metrics and charts, including the comparison of strategies.

Details (confirmed in P9):
- Start: `uv run streamlit run dashboard/app.py`; the API address comes from the environment variable `RFQ_API_URL` (default `http://127.0.0.1:8000`); the back-end config is not read. Streamlit is in the dependency group `dashboard`. Charts use Altair.
- The run is chosen in the sidebar (`GET /runs`) and synced to the URL parameter `?run=`; after an upload the new run is selected automatically.
- Upload: optional strategy, batch_size, review (checked by default on the page), limit; dataset is not offered. 422 / 409 show the server's text; 409 (real provider) hints that the server needs `--allow-real`.
- Progress: poll `GET /runs/{id}` every 2 seconds until the status is not `running`. Progress = (completed + failed batches) / total batches; all batches done but still `running` shows "finalizing"; `awaiting_review` shows "finalized, worksheet downloadable, N red rows still awaiting review"; `running` with `lease_active=false` shows "background stopped, can be resumed" with a "Resume" button (`POST /resume`).
- Overview: every `RunSummary` metric and the run parameters; before finalize there is no summary, only progress.
- Worksheet: band and review-status filters and pagination are all done by the API (50 / 100 / 500 rows per page); whole rows colored by band, same colors as Excel; a row can be selected to open its detail; after finalize the xlsx can be downloaded.
- Row detail: conclusions and price reference, supersession chain and all OE candidates, price comparison across sites (marking selected, outliers and MSRP), cost waterfall (material → … → FOB → … → landed cost), confidence signals and caps (signal values only; weights stay in the server config), the four paths' complete reasoning and evidence, review history.
- Review queue: actionable only on runs with review enabled and status `awaiting_review`, listing red unreviewed rows (`band=red&status=unreviewed`). Accept / reject / modify price refresh after a 200; modify OE polls the row every 2 seconds after a 202 until `review_status` becomes `modified`, for at most 120 seconds; if the worker stops in the meantime (`lease_active=false`), prompt to resubmit the same decision.
- Evaluation (P10): a 404 shows "no evaluation yet" with a hint to run `rfq eval --run <id>`; with an evaluation, show the per-stage metric tables, the per-band table, ECE, AUROC, the reliability diagram (with a diagonal reference line), the coverage-accuracy curve (marking the threshold for the target accuracy), cost and time; strategy comparison: pick other runs, fetch each evaluation (404 marked "not evaluated"), and build the comparison table and overlaid curves on the page (the comparison table includes the three injection columns); when the run has injection records, show an extra "injected errors" section: share held back, injected rows left in green, AUROC and rows per band by error type.
- No caching of API results (review changes the data); API errors always show the server's detail.

---

## 16. Documentation

- `README.md` (English) / `README.zh-CN.md` (Chinese): project overview, architecture diagram, quick start (3 commands in fake mode), configuration, evaluation results summary.
  - Quick start (confirmed in P11): `uv sync` → `uv run rfq data generate` → `RFQ_LLM_PROVIDER=fake uv run rfq run --input data/samples/rfq_sample_20.xlsx`, no `.env` needed; the three commands sit between `<!-- quickstart:begin -->` / `<!-- quickstart:end -->`, and a test runs them in a fresh copy as acceptance (exit codes, outputs, clean working tree afterwards).
  - Separate English and Chinese versions (confirmed after the P11 review): `README.md` is English (shown by GitHub by default), `README.zh-CN.md` is Chinese; the two link to each other at the top with the same content, and a test checks that their quick-start commands are identical. The overview does not include the author's personal background; it only says the project is currently for learning and exchange, plans to grow into a version working with real enterprise data, and that anyone interested in collaborating can reach out through a GitHub issue.
  - There are also "More usage" (evaluation, rescore and strategy comparison, error injection, review, resume / rerun-failed, single agent, API, Dashboard, MCP) and "Development" sections. Every command and output in the README was actually run in fake mode; for real providers it only shows the refusal without `--allow-real`. The evaluation summary uses only real numbers recorded in `docs/plans/`.
- `RFQ-SPEC.md` (Chinese, the only authoritative version) / `RFQ-SPEC.en.md` (English translation): the two link to each other at the top, and the English version states at the start that the Chinese version wins. When the spec changes, the English version is updated in the same commit; a test checks that both have the same section numbers, the same number of code blocks, and the same code apart from comments inside code blocks (the English version translates comments in code blocks and keeps the code as is). The English version ends with a Chinese–English term table (taken from GLOSSARY.md).
- `docs/design-notes.md`: **generate only the template; the content is written by me**; do not invent details of the original system. Template sections:
  1. 原系统概述（平台版本） (Overview of the original system, platform version)
  2. 为什么拆成四个 agent (Why four agents)
  3. 阶段契约的设计 (Design of the stage contracts)
  4. 实体消解的难点 (What makes entity resolution hard)
  5. 分批与容错 (Batching and fault tolerance)
  6. 置信度策略的选择与评估结果 (Choosing the confidence strategy, and evaluation results)
  7. 平台方案 vs 自建方案的取舍 (Platform vs self-built trade-offs: development speed, control, observability, cost, testability, vendor lock-in)
  8. 如果上生产还需要什么 (What production would still need)

---

## 17. Phases and acceptance criteria

| Phase | Content | Acceptance criteria |
|---|---|---|
| P0 | Project skeleton, config loading, LLM factory, FakeLLM, call tracking, CLI framework | `uv run pytest` passes; `rfq --help` works; the FakeLLM returns structured output |
| P1 | Synthetic data generator, ground truth, SQLite internal database, MCP server, mock tools | Generate 200 rows with difficulty ratios matching §6.3 (±3%); MCP Inspector can call every tool |
| P2 | `oe_interchange` agent | Tests pass in fake mode; a real LLM runs 20 rows and outputs JSONL for my manual check |
| P3 | `oe_msrp` agent | Same as above |
| P4 | `retail_price` agent | Same as above; outlier detection unit tests pass |
| P5 | `should_cost` agent | Same as above; unit tests for each formula term pass |
| P6 | Entity resolution + the three confidence strategies | Many-to-one and one-to-many tests pass; all three strategies can be switched and run |
| P7 | Orchestrator: main graphs, parallelism, batching, checkpoints, resume, rerun-failed, human review | The §13 kill/resume acceptance passes; `--review` mode is interactive |
| P8 | Excel output + FastAPI | Worksheet colors correct; every endpoint has tests |
| P9 | Streamlit Dashboard | All six pages work and depend only on the API |
| P10 | Evaluation scripts + strategy comparison | Eval report and charts generated; a real LLM runs the three strategies on 200 rows and outputs the comparison table (report the expected cost before running) |
| P11 | README + design-notes template | In a new environment, the three README commands run through in fake mode |

Note on order: P2 comes first because the price agents depend on OE resolution; `should_cost` does not depend on the price agents and may swap places with P3/P4 if needed.

---

## 18. Configuration example

`config/settings.yaml`:

```yaml
llm:
  provider: deepseek            # deepseek | anthropic | qwen | fake
  base_url: https://api.deepseek.com
  model: <实施时查官方文档确认>
  temperature: 0
  max_concurrency: 8
  pricing_per_million:          # for cost tracking; fill in from official prices during implementation
    input: 0.0
    output: 0.0
  roles:
    judge:
      provider: deepseek
      model: <可与主模型不同>

pipeline:
  batch_size: 100
  structured_output_retries: 2
  retail_outlier_threshold: 0.40
  spec_weight_tolerance: 0.05     # should_cost: relative error for internal vs external weight agreement (P5)

confidence:
  strategy: evidence_plus_judge # evidence | evidence_plus_self | evidence_plus_judge (default since the P10 error injection experiment)
  bands:
    green_min: 80
    yellow_min: 50
  self_samples: 2
  self_weight: 0.3              # P6: weight of the LLM self-assessment signal in the final score
  judge_band: [50, 100]         # added in P10: the judge also reviews green rows (it can only lower scores)
  judge_weight: 0.5             # P6: weight of the judge score in the final score
  evidence:                     # P6: weights and per-level values of the evidence signals (§12.2), tuned in P10
    weights:
      oe_match: 0.25
      chain: 0.10
      price_sources: 0.20
      retail_dispersion: 0.15
      msrp_retail: 0.10
      spec_source: 0.20
    oe_match: {exact: 1.0, normalized: 0.9, interchange: 0.7, llm_inferred: 0.2, manual: 1.0}
    spec_source: {both_agree: 1.0, internal: 0.8, external: 0.5, conflict: 0.3, missing: 0.0}
    chain_truncated: 0.3
    msrp_retail_suspicious_ratio: 1.2
    price_sources_full: 4
    msrp_retail: {at_most_msrp: 1.0, up_to_suspicious: 0.5, above: 0.0, missing: 0.5}
    single_quote_dispersion: 0.5
```

The two placeholders are kept as in the Chinese original: `<实施时查官方文档确认>` means "check the official docs during implementation", and `<可与主模型不同>` means "may differ from the main model".

`llm.roles` also has `self_eval` (P6): same model as the main one, temperature 0.7.

```yaml
storage:                          # P7
  db_path: data/rfq.db            # result database (business tables)
  checkpoint_path: data/checkpoints.db   # LangGraph checkpoints, a separate file
  upload_dir: data/uploads        # P8: RFQs uploaded through the API
  lease_ttl_s: 60                 # P8: a run lease whose heartbeat is older than this many seconds has expired
  lease_heartbeat_s: 10           # P8: interval for refreshing the heartbeat while holding the lease; lease_ttl_s must be more than twice this
```

SQLite ≥ 3.51.2 is required (P8): 3.51.0 / 3.51.1 can deadlock when one thread in a process opens a database and another thread closes the connection. Connections opened through `store.db` are serialized in code, and a warning is given when these two versions are detected.

`config/cost_model.yaml`:

```yaml
# All rates are illustrative and do not represent any real tariff or quote
scrap_rate: 0.05
labor_rate_per_hr: 0.0
machine_rate_per_hr: 0.0
overhead_rate: 0.0
sga_rate: 0.0
profit_rate: 0.0
insurance_rate: 0.0
mfn_duty_rate: 0.0
additional_tariff_rate: 0.0
mpf_rate: 0.0
brokerage_per_entry: 0.0
units_per_shipment: 0
categories:
  thermostat:
    cycle_time_min: 0.0
    packaging: 0.0
    freight_per_unit: 0.0
    inland_freight_per_unit: 0.0
  water_pump:
    cycle_time_min: 0.0
    packaging: 0.0
    freight_per_unit: 0.0
    inland_freight_per_unit: 0.0
```

(In P1, Claude Code fills in a set of reasonable illustrative values, using the same parameters as the synthetic data's ground truth.)

---

## Appendix: Chinese–English terms

Terms as defined in [GLOSSARY.md](GLOSSARY.md) (Chinese), for reading the code, the Chinese spec and `docs/plans/`.

| Chinese | English | Meaning in one line |
|---|---|---|
| 询价单 | RFQ | The Excel a customer sends, one part to quote per row |
| 询价行 | RFQ line | One row of an RFQ, with a unique row id |
| 客户件号 | customer part number | Part number written by the customer, raw and normalized |
| 标准化 | normalization | Full-width → half-width, upper case, drop spaces and `- _ . /` before comparing |
| 本公司件号 | our part number | Number in our part master data, given in the RFQ or found in the cross-reference |
| 对照表 | cross-reference | Known customer part number → our part number pairs in the internal database |
| OE 号 | OE number | Original-equipment number, the key for MSRP and retail lookups |
| 替代链 | supersession chain | Old-to-new OE numbers of the same part |
| 当前 OE 号 | current OE | Last number of the supersession chain |
| 主号 | primary OE | The OE chosen for quoting in a one-to-many case |
| OE 解析结果 | OE resolution | resolved / multiple / not_found |
| 一对多 / 多对一 | one-to-many / many-to-one | One customer number to several OEs / several customer numbers to one OE |
| 共用 OE 组 | shared-OE group | Resolved rows with the same current OE, sharing price results |
| 匹配类型 | match type | How an OE candidate was found (OE resolution only) |
| 厂商指导价 | MSRP | OE maker's suggested retail price, USD only |
| 停产 | discontinued | Marked discontinued on the maker's price page |
| 回退 | fallback | Using an older chain number's price when the current one has none |
| 站点 | site | One of the five retail websites |
| 零售报价 | retail quote | One matching listing at a site |
| 原价 / 折扣价 / 实际价 | list price / sale price / effective price | Effective = sale price if any, else list price |
| 零售标题件号 | listed part number | Part number as written in a retail title |
| 匹配质量 | match quality | exact / normalized / fuzzy (retail only) |
| 离群报价 | outlier | Quote too far from the row's median |
| 参考零售价 | selected retail quote | The quote chosen as the pricing reference |
| 应有成本 | should-cost | Estimated FOB cost of making the part ourselves |
| 到岸成本 | landed cost | Should-cost plus freight, insurance, duty, MPF, brokerage, inland freight |
| 成本栈 | cost breakdown | Item-by-item amounts from material to landed cost |
| 规格 | spec | Category, material, weight, cycle time |
| 品类 | category | thermostat or water pump |
| 材质 | material | Main material, as named in the internal database |
| 规格来源 | spec source | both_agree > internal > external > conflict; missing |
| 数据不足 | insufficient data | Inputs really missing, should-cost cannot be estimated |
| 参考价区间 | price reference | MSRP, reference retail price and landed cost side by side |
| 内部库 | internal DB | Our master data, cross-reference, material prices, rates (via MCP) |
| 外部页面 | external page | Noisy text from the mock web tools |
| 证据 | evidence | Source, page or record id, and key snippet |
| 原文核对 | grounding | Extracted values must appear in the page text |
| 行失败 | row failure | A row could not be processed at a stage |
| 置信度 | confidence score | 0–100 confidence that the row is usable as is |
| 行正确 | row correct | Every stage of the row is correct |
| 可直接使用 | usable as is | Row correct, OE resolved, retail not all outliers |
| 证据信号 | evidence signal | Deterministic 0–1 signals behind the evidence score |
| 封顶 | gate (cap) | Rule capping a score at the red or yellow ceiling |
| LLM 自评 | self-assessment | `evidence_plus_self`: the LLM answers and states its confidence |
| judge 复核 | judge review | `evidence_plus_judge`: another model agrees or not; can only lower scores |
| 需复核原因 | review reasons | Why a person should look at the row |
| 档位 | band | green / yellow / red |
| 人工复核 | review | A person accepts, modifies or rejects red rows |
| 待复核 | awaiting review | Finalized, red rows still to review |
| 运行 | run | One complete processing of one RFQ |
| 批 | batch | Rows processed and stored together (default 100) |
| 批失败 | failed batch | A batch that could not finish; nothing stored |
| 收尾 | finalize | Resolution, confidence, review and export over the whole table |
| 领头行 | price leader | First resolved row of a current OE that runs the price agents |
| 续跑 | resume | Continue an interrupted run |
| 重新打分 | rescore | New run on copied agent results with another strategy |
| 注入行 | injected row | Row made wrong on purpose in the injection experiment |
| 评估 | evaluation | Comparison of a run with ground truth |
| 重跑失败 | rerun failed | Rerun failed batches and rows, then finalize again |
| 复核状态 | review status | accepted / modified / rejected |
| 运行租约 | run lease | Only one process drives a run at a time |
| 心跳 | heartbeat | Timestamp the lease holder refreshes |
| worker | worker | Background process the API starts for a run |
