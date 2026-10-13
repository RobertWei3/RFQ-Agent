# RFQ-Agent 实施文档（给 Claude Code）

> 仓库目录：`RFQ-Agent/`
> 本文档是唯一的需求来源。实施时按第 15 节的 Phase 顺序推进，**每完成一个 Phase 就停下**，汇报做了什么、测试结果、下一步计划，等我确认后再继续。

---

## 0. 给 Claude Code 的工作约定

1. 严格按 Phase 顺序推进，不要跨 Phase 提前实现。
2. 每个 Phase 都必须有 pytest 测试，并且在 **fake LLM 模式**下全部通过（不花 API 费用）。
3. 需要调用真实 LLM 的冒烟测试，单次规模控制在 ≤20 个零件，运行前先告诉我预计调用次数。
4. 所有外部数据源都是 mock。不要写任何真实网站爬虫。
5. 第三方库 API 不确定时（LangGraph、MCP SDK、langchain-mcp-adapters、DeepSeek 模型 ID），先查官方文档再写，不要凭记忆。
6. 遇到本文档没写清楚的业务问题，**停下来问我**，不要自行编造业务规则。
7. 代码风格：类型注解齐全，Pydantic v2，函数短小，模块之间只通过本文档定义的 schema 交互。
8. **代码中所有注释（comments）和 docstring 一律使用英文**，不要出现中文注释。

---

## 1. 背景与目标

这是一个**学习与交流用的项目**。参考的原系统是一个已在生产中使用、基于 agent 平台搭建的 RFQ（询价）自动报价流程；本项目用 Python + LangGraph 从零自建一个简化版，目的是：

- 吃透原系统的设计：多 agent 拆分、阶段契约、实体消解、分批容错、置信度分流、人工复核。
- 对比"平台方案"和"自建方案"的取舍。
- 用可量化的评估结果（覆盖率、准确率、置信度校准、成本）检验设计选择。

**业务流程**：客户发来一份 Excel 询价单（几百到几千个客户件号）→ 系统找到每个件号对应的 OE（原厂）号 → 查询 OE 厂商指导价（MSRP）和零售价 → 估算应有成本（should-cost）→ 输出一份定价工作表，供定价团队决策。

## 2. 非目标

- 不做真实爬虫，不接真实 Azure SQL。
- 不做用户登录和多租户。
- 第一版前端用 Streamlit；之后会换成 React，所以**前端只能通过 FastAPI 访问后端**，不得直接 import 后端模块。

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.11+ |
| 依赖管理 | uv |
| Agent 编排 | LangGraph（StateGraph、并行 fan-out、checkpointer、interrupt） |
| LLM 接入 | LangChain chat model 抽象 |
| 默认 LLM | DeepSeek（OpenAI 兼容接口，`base_url` 和模型 ID 写在配置里，实施时查官方文档确认当前模型 ID） |
| 测试用 LLM | 自写的 FakeLLM（确定性、零成本，见 §8） |
| 内部数据 | SQLite（代替 Azure SQL），通过 **MCP server** 暴露（Python 官方 `mcp` SDK） |
| MCP 客户端 | `langchain-mcp-adapters` |
| 结构化输出 | Pydantic v2 + 模型 structured output；校验失败自动重试 |
| 持久化 | SQLite（业务结果表 + LangGraph checkpoint） |
| API | FastAPI |
| Dashboard | Streamlit（只调 FastAPI） |
| Excel | openpyxl（按置信度档位着色） |
| 测试 | pytest |
| 配置 | YAML + `.env`（API key 只放 `.env`，`.env` 写进 `.gitignore`） |

---

## 4. 架构总览

```
Excel 询价单
   │
   ▼
[intake] 确定性解析与件号标准化
   │
   ▼
[oe_interchange agent] 客户件号 → OE 号（含新旧替代链）
   │
   ├──────────────┬───────────────┐   ← 并行 fan-out
   ▼              ▼               ▼
[oe_msrp agent] [retail_price agent] [should_cost agent]
   │              │               │
   └──────────────┴───────────────┘
   ▼
[entity_resolution] 多对多消解，合并成每行一条记录（确定性）
   │
   ▼
[confidence] 0–100 置信度 + 绿/黄/红档位（策略可切换）
   │
   ▼
[output] 写入结果库、定价工作表 Excel（收尾完成即可下载）→ FastAPI → Dashboard
   │
   ▼
[review]（仅 --review 模式）在已收尾的运行上复核红档行（interrupt）；每条决定后回到实体消解重新汇总，工作表随之更新
```

- 四个 subagent：`oe_interchange`、`oe_msrp`、`retail_price`、`should_cost`。每个都能**单独运行和测试**（有独立的 CLI 子命令），最后再由 orchestrator 串起来。
- orchestrator 以**批**为单位运行四个 agent：每批 100 行，一批跑完、结果落库后再启动下一批；全部批完成后再对全表做实体消解、置信度和人工复核（详见 §13）。

---

## 5. 目录结构

```
RFQ-Agent/
├── RFQ-SPEC.md
├── README.md
├── pyproject.toml
├── .env.example
├── config/
│   ├── settings.yaml          # LLM、批大小、并发、阈值、置信度策略
│   └── cost_model.yaml        # should-cost 和到岸成本的示意费率
├── data/
│   ├── generated/             # 合成数据（生成器输出，git 忽略）
│   └── samples/               # 小样例询价单（入库）
├── src/rfq_agent/
│   ├── schemas.py             # 所有 Pydantic 契约（§7）
│   ├── config.py
│   ├── llm/
│   │   ├── factory.py         # 按配置返回 DeepSeek / Claude / Qwen / Fake
│   │   ├── fake.py            # FakeLLM
│   │   └── tracking.py        # token、成本、耗时记录回调
│   ├── data_gen/              # 合成数据与 ground truth 生成器（§6）
│   ├── tools/                 # mock 外部数据源（§9）
│   ├── mcp_server/            # 内部数据库 MCP server（§10）
│   ├── intake/
│   ├── agents/
│   │   ├── oe_interchange.py
│   │   ├── oe_msrp.py
│   │   ├── retail_price.py
│   │   └── should_cost.py
│   ├── resolution/            # 实体消解
│   ├── confidence/            # 三种置信度策略
│   ├── orchestrator/          # LangGraph 主图、分批、检查点、复核
│   ├── output/                # Excel 写出
│   ├── store/                 # SQLite 结果库访问层
│   ├── api/                   # FastAPI
│   ├── eval/                  # 评估脚本（§14）
│   ├── costing/               # 应有成本与到岸成本公式（确定性，§11.4）
│   ├── partno.py              # 件号标准化（intake、工具、MCP、匹配共用）
│   ├── pricing.py             # MSRP 判对的容差（置信度与评估共用）
│   ├── trace.py               # 本地运行 trace（每步一条 JSONL）
│   └── cli/                   # Typer CLI 入口，每组命令一个模块
├── dashboard/                 # Streamlit；只经 HTTP 调 API，不 import rfq_agent（P9）
│   ├── app.py                 # 入口：页面导航、侧栏选运行
│   ├── api_client.py          # httpx 封装的 API 客户端
│   ├── charts.py              # 纯函数：JSON → 表格与图表数据
│   └── views/                 # 每页一个模块
├── docs/
│   └── design-notes.md        # 设计说明模板（§16）
└── tests/
```

---

## 6. 合成数据

### 6.1 生成器

`rfq data generate --n-parts 200 --seed 42`，可放大到 `--n-parts 8000` 用于分批压测。所有随机过程固定 seed，可复现。

品类：**节温器（thermostat）**和**水泵（water pump）**，品牌和车型全部虚构。

### 6.2 生成的实体

| 实体 | 说明 |
|---|---|
| `our_parts` | 本公司零件主数据：`our_part_no`、description、品类、材质、重量 kg、cycle time（内部库，经 MCP 访问） |
| `customer_xref` | 客户件号 ↔ 本公司件号的已知对应（部分有、部分没有） |
| `oem_catalog_pages` | 模拟 OEM 目录的**文本页面**：含 OE 号、适配车型、"superseded by / replaces" 替代关系，带噪声（多余文字、格式不统一） |
| `interchange_pages` | 模拟互换查询结果页：输入一个件号，返回若干候选 OE 号的文本片段 |
| `msrp_pages` | 模拟 OE 厂商官网价格页：OE 号、MSRP、货币、有时标注"discontinued" |
| `retail_listings` | 5 个虚构零售站点的商品列表文本：标题、件号（可能写法不同）、原价、折扣价、是否有货 |
| `material_prices` | 材料价格指数（如铝、铸铁、橡胶，单位 $/kg） |
| `ground_truth` | 每个客户件号的正确答案：正确的当前 OE 号、完整替代链、正确 MSRP、各站点正确售价、真实材质与重量 |

### 6.3 有意埋入的难点（比例可在生成器参数里调）

| 难点 | 默认比例 |
|---|---|
| 替代链（长度 2–3，旧号 → 新号） | 15% |
| 一个客户件号对应多个 OE 号（一对多） | 10% |
| 多个客户件号对应同一个 OE 号（多对一） | 10% |
| 找不到 OE 号 | 8% |
| 部分零售站点缺价 | 15% |
| 某站点价格明显离群（价格冲突） | 10% |
| 零售标题里的件号写法不同（大小写、横杠、空格） | 20% |
| 内部库缺材质或重量，需要从外部页面抽取 | 15% |
| 内部数据与外部页面数据不一致 | 5% |
| 输入表格式脏乱（多余空格、全角字符、空行） | 5% |

### 6.4 输入询价单格式

Excel，单个 sheet。列名允许大小写和空格差异，intake 负责统一：

| 列 | 必填 | 说明 |
|---|---|---|
| `customer_part_no` | 是 | 客户件号 |
| `our_part_no` | 否 | 如果客户已经给出本公司对应件号就填，否则留空 |
| `description` | 否 | 描述，常常缺失 |
| `annual_qty` | 否 | 年用量 |
| `customer_name` | 否 | 客户名称 |

生成器同时输出 `data/samples/rfq_sample_20.xlsx`（20 行）和 `data/generated/rfq_200.xlsx`。

---

## 7. 数据契约（`schemas.py`）

所有阶段之间**只传 Pydantic 对象**。每个模型带 `schema_version: str` 字段，默认值为 `"1.0"`；字段有增改的模型单独升版本（目前 `OEResolution` 为 `"1.2"`，`OECandidate`、`MSRPResult`、`RetailResult`、`ShouldCostResult`、`WorksheetRow`、`RunRecord`、`ReviewDecision`、`RunSpec`、`RunEval` 为 `"1.1"`，见各模型注释）。

```python
class RFQLine(BaseModel):
    row_id: int
    customer_part_no_raw: str
    customer_part_no: str            # 标准化后
    our_part_no: str | None
    description: str | None
    annual_qty: int | None
    customer_name: str | None

class Evidence(BaseModel):
    source: str                      # 例如 "oem_catalog", "retailer_3", "internal_db"
    ref: str                         # 页面 ID 或记录 ID
    snippet: str | None              # 关键原文片段（≤200 字符）

class OECandidate(BaseModel):     # schema_version "1.1"（P7 新增 manual：人工复核输入的 OE 号）
    oe_part_no: str
    brand: str | None
    match_type: Literal["exact", "normalized", "interchange", "llm_inferred", "manual"]
    evidence: list[Evidence]

class OEResolution(BaseModel):     # schema_version "1.2"（P2 新增 our_part_no / our_part_source；P6 新增 chain_truncated）
    row_id: int
    our_part_no: str | None = None   # 本公司件号：询价单给出或经 MCP 对照表查到
    our_part_source: Literal["given", "xref", "none"] = "none"
    status: Literal["resolved", "multiple", "not_found"]
    current_oe: str | None           # 沿替代链走到的当前有效 OE 号
    supersession_chain: list[str]    # 例如 ["OLD-1", "MID-2", "NEW-3"]
    chain_truncated: bool = False    # 补链到跳数上限时仍有未追的号码（置信度信号用）
    candidates: list[OECandidate]    # 保留全部候选
    reasoning: str                   # LLM 给出的判断理由

class PriceQuote(BaseModel):
    source: str
    price_type: Literal["msrp", "retail"]
    matched_part_no: str
    list_price: float | None
    sale_price: float | None
    discount_pct: float | None
    effective_price: float | None    # 有折扣价用折扣价，否则用原价
    currency: str = "USD"
    in_stock: bool | None
    match_quality: Literal["exact", "normalized", "fuzzy"]
    reasoning: str
    evidence: list[Evidence]

class MSRPResult(BaseModel):       # schema_version "1.1"（P3 新增 reasoning）
    row_id: int
    oe_part_no: str | None
    quote: PriceQuote | None
    status: Literal["found", "not_found", "discontinued"]
    reasoning: str = ""              # 结论原因，含替代链回退、未查原因等

class RetailResult(BaseModel):     # schema_version "1.1"（P4 新增 reasoning）
    row_id: int
    oe_part_no: str | None
    quotes: list[PriceQuote]         # 每个站点最多一条（选最精确的匹配）
    selected: PriceQuote | None      # 用于定价参考的那一条
    outliers: list[str]              # 被判定为离群的来源
    reasoning: str = ""              # 整行结论：未查原因、站点搜索失败、离群偏离、选中理由等

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

class ShouldCostResult(BaseModel): # schema_version "1.1"（P5 新增 category / cycle_time_min / evidence）
    row_id: int
    category: str | None = None      # 计算所用品类：内部库，或本公司件号未知时取自 OEM 目录页 / 询价单描述
    material: str | None             # 实际用于计算的材质 key（冲突时为内部值）
    weight_kg: float | None          # 实际用于计算的重量（冲突时为内部值）
    cycle_time_min: float | None = None  # 零件自身值或品类默认值（reasoning 注明）
    spec_source: Literal["internal", "external", "both_agree", "conflict", "missing"]
    breakdown: CostBreakdown | None
    status: Literal["estimated", "insufficient_data"]
    reasoning: str
    evidence: list[Evidence] = []    # 内部库记录、外部规格页、目录页

class ConfidenceResult(BaseModel):
    row_id: int
    score: int                       # 0–100
    band: Literal["green", "yellow", "red"]
    strategy: str
    signals: dict[str, float]        # 各项证据信号，便于解释
    explanation: str

class RowFailure(BaseModel):
    # 某一行在某个阶段无法处理时的统一输出（不抛异常，其他行照常处理）
    row_id: int
    stage: str                       # 失败的阶段，例如 "search_chain"、"upstream_oe"
    error: str                       # 失败原因

# 内部库 MCP 工具的返回（§10，P5 起作为正式契约，agent 与 MCP server 共用）
class PartSpec(BaseModel):           # get_part_spec
    found: bool
    our_part_no: str
    category: str | None = None
    description: str | None = None
    material: str | None = None      # 主数据不完整时为空
    weight_kg: float | None = None
    cycle_time_min: float | None = None

class MaterialPrice(BaseModel):      # get_material_price；内部价格必须标明币种，只采用 USD
    found: bool
    material: str
    price_per_kg: float | None = None
    currency: str | None = None
    as_of: str | None = None

class MaterialList(BaseModel):       # list_materials
    materials: list[str]

class WorksheetRow(BaseModel):     # schema_version "1.1"（P7 新增 review_status）
    # 扁平的列模型，Excel、行列表 API、Dashboard 表格共用；列见 §12.1
    row_id: int
    customer_part_no_raw: str
    customer_part_no: str
    our_part_no: str | None
    oe_status: Literal["resolved", "multiple", "not_found"] | None   # OE 行失败时为空
    current_oe: str | None
    supersession_chain: list[str]
    oe_candidates: list[str]         # 一对多时各候选的当前号（只列原文核对得上的，不含 llm_inferred）
    shared_oe_rows: list[int]        # 共用同一 OE 的其他行
    price_source_row: int | None     # 价格结果取自哪一行（多对一复用）
    shared_prices_disagree: bool     # 共用 OE 的行价格结论不一致
    msrp: float | None
    msrp_status: Literal["found", "not_found", "discontinued"] | None   # OE 不是 resolved 时为空
    retail_prices: dict[str, float | None]   # 站点 → effective_price
    selected_retail_price: float | None
    selected_retail_source: str | None
    retail_outliers: list[str]
    should_cost_fob: float | None
    landed_cost: float | None
    confidence_score: int | None     # 置信度计算前为空
    band: Literal["green", "yellow", "red"] | None
    review_reasons: list[str]
    failures: list[str]              # "路:阶段:原因"
    reasoning_summary: dict[str, str]        # 每路 reasoning 前 200 字符
    review_status: Literal["accepted", "modified", "rejected"] | None = None   # 人工复核结论（§13）

class ResolvedRow(BaseModel):
    # 一行的完整结果：行详情与置信度计算用
    line: RFQLine
    worksheet: WorksheetRow
    oe: OEResolution | RowFailure
    msrp: MSRPResult | RowFailure    # 多对一时为代表行的结果
    retail: RetailResult | RowFailure
    should_cost: ShouldCostResult | RowFailure
    confidence: ConfidenceResult | None = None

# 编排运行与人工复核（§13，P7 新增）；CLI、编排器与之后的 API 共用
class RunSpec(BaseModel):            # 建运行时固定的参数，resume / rerun-failed 沿用；schema_version "1.1"（P10 新增 rescored_from）
    input: str
    dataset_dir: str
    batch_size: int
    strategy: Literal["evidence", "evidence_plus_self", "evidence_plus_judge"]
    review: bool
    rescored_from: str | None = None # 复制来的运行（`rfq rescore`、`rfq eval inject`）：复制了哪个运行的四路结果（§13）

class RunRecord(BaseModel):
    run_id: str
    created_at: str
    spec: RunSpec
    status: Literal["running", "awaiting_review", "completed"]   # awaiting_review：已收尾、工作表可下载、还有红档行待复核
    finished_at: str | None = None   # P8：最近一次收尾完成的时间（汇总耗时用）

class BatchRecord(BaseModel):
    batch_id: int
    row_ids: list[int]
    rerun_rows: list[int] | None = None   # 待执行的重跑只跑这些行（不能是空列表）；None 表示全批
    status: Literal["pending", "running", "completed", "failed"]
    attempts: int                    # 开始执行的次数（被打断后恢复也算一次）
    error: str | None = None

    def rows_to_run(self) -> list[int]:   # 下一次执行要跑的行：rerun_rows，为 None 时是全批 row_ids
        ...

class ReviewQueue(BaseModel):        # P8：interrupt() 的值，一次交出整个待复核队列
    run_id: str
    row_ids: list[int]               # 红档、未复核的行，按 row_id

class ReviewPrompt(BaseModel):       # CLI 复核回调的输入：队列中的下一行
    run_id: str
    remaining: int                   # 含本行在内还待复核的红档行数
    row: ResolvedRow

class ReviewInput(BaseModel):        # P8：API 提交复核的请求体（行号取自路径）
    action: Literal["accept", "modify_oe", "modify_price", "reject"]
    oe: str | None = None            # modify_oe 必填
    msrp: float | None = None        # modify_price 至少填一个，> 0
    retail: float | None = None
    note: str | None = None

class ReviewDecision(ReviewInput):   # Command(resume=...) 的值：对队列中某一行的决定
    row_id: int                      # P8 新增；不在当前队列中的决定被忽略

class ReviewEntry(BaseModel):        # review_log 的一行
    row_id: int
    action: Literal["accept", "modify_oe", "modify_price", "reject"]
    oe_before: str | None = None
    oe_after: str | None = None
    msrp_before: float | None = None
    msrp_after: float | None = None
    retail_before: float | None = None
    retail_after: float | None = None
    note: str | None = None
    snapshot: ResolvedRow            # 复核前的整行
    ts: str = ""                     # 写入 review_log 的时间；为空时由结果库填当前时间

# 运行租约与 LLM 用量（P8 新增）：结果库、编排器、汇总之间共用
class RunLease(BaseModel):           # 谁在驱动运行（§13）
    owner: str                       # 进程 token：pid@host:随机
    heartbeat: float                 # 上次心跳（epoch 秒）

class LLMUsage(BaseModel):           # 一个运行的 llm_calls 合计
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None           # 有未定价的成功调用时为空

# API 响应（§15.1，P8 新增）
class RunProgress(BaseModel):        # 直接读 batch_results
    batches_total: int
    pending: int
    running: int
    completed: int
    failed: int

class RunSummary(BaseModel):         # GET /runs/{id} 与工作表第二个 sheet 共用
    rows: int
    bands: dict[Band, int]           # green / yellow / red
    reviews: dict[ReviewStatus, int] # accepted / modified / rejected
    review_pending: int              # 复核运行中还未复核的红档行
    coverage: dict[Literal["oe", "msrp", "retail", "should_cost"], float]   # 按系统结果（不含人工改价）
    rows_with_failure: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    llm_cost_usd: float | None       # 有未定价模型时为空
    elapsed_s: float | None          # finished_at − created_at（墙钟，含 kill 后的停机时间）

class RunDetail(BaseModel):
    run: RunRecord
    progress: RunProgress
    summary: RunSummary | None       # 收尾前为空
    lease_active: bool               # 有进程正在驱动（租约未过期）

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
    done: bool                       # False：已接受、后台处理中（202）
    row: ResolvedRow | None          # done 时为复核后的行

# 评估（§14，P10 新增）；`GET /runs/{id}/eval` 的响应
class OEEval(BaseModel):
    coverage: float                  # resolved / 全部行
    accuracy: float | None           # resolved 行中 current_oe 等于主号的比例
    conclusion_accuracy: float       # 全部行的 OE 结论正确率（含正确的 not_found / multiple）
    chain_accuracy: float | None     # 系统与 truth 都 resolved 的行中替代链完全相同的比例

class MSRPEval(BaseModel):
    coverage: float                  # found 或 discontinued / 全部行
    accuracy: float | None           # 有价的行中相对误差 ≤1% 的比例
    status_accuracy: float
    mean_rel_error: float | None

class SiteEval(BaseModel):
    expected: int                    # truth 中 OE 为 resolved、且在该站点有挂牌的行（其余行按规则不查零售价）
    quoted: int                      # 该站点有报价的行
    found: int                       # expected 中拿到报价的行
    coverage: float | None           # found / expected

class RetailEval(BaseModel):
    sites: dict[str, SiteEval]
    selected_accuracy: float         # 参考零售价站点正确（truth 预期不选时不选也算对）
    outlier_precision: float | None  # 按（行，站点）对，只看 truth 为 resolved 的行
    outlier_recall: float | None

class ShouldCostEval(BaseModel):
    spec_accuracy: float             # 品类、材质、重量（≤1%）
    landed_mape_part: float | None   # 零件自身 cycle time 的行
    landed_mape_default: float | None   # 品类默认 cycle time 的行
    n_part: int
    n_default: int

class BandEval(BaseModel):
    rows: int
    usable: int                      # 可直接使用的行
    row_correct: int

class ReliabilityBin(BaseModel):     # 区间 [lo, hi)，最后一个含 100
    lo: int
    hi: int
    rows: int
    mean_score: float | None         # 分数 / 100 的平均
    usable_rate: float | None

class CurvePoint(BaseModel):         # 只自动处理分数 ≥ threshold 的行
    threshold: int
    coverage: float
    accuracy: float | None           # 按“可直接使用”

class ThresholdPick(BaseModel):      # 达到目标准确率的最低阈值
    target: float
    threshold: int | None
    coverage: float | None

class CalibrationEval(BaseModel):
    row_correct_rate: float
    usable_rate: float
    ece: float
    auroc: float | None              # 只有一类标签时为空
    bands: dict[Band, BandEval]
    red_row_correct_rate: float | None   # 红档中行正确的比例（被正确拦下的行）
    reliability: list[ReliabilityBin]
    curve: list[CurvePoint]
    thresholds: list[ThresholdPick]  # 目标 0.90 / 0.95 / 0.99

class AgentUsage(BaseModel):         # llm_calls 按 agent 分组
    agent: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None           # 有未定价的成功调用时为空

class UsageEval(BaseModel):
    agents: list[AgentUsage]
    models: dict[str, str]           # 角色 → provider/model（取自 llm_calls）
    cost_usd: float | None           # 本运行的调用
    source_cost_usd: float | None    # 复制来的运行：复制链上所有源运行的调用（agent 成本）；否则为空
    total_cost_usd: float | None     # 两者之和
    elapsed_s: float | None
    cost_per_1k_rows: float | None   # total_cost_usd / 行数 × 1000

class RowEval(BaseModel):
    row_id: int
    score: int | None
    band: Band | None
    row_correct: bool
    usable: bool                     # 可直接使用

class InjectionEval(BaseModel):     # 错误注入实验（§14）；InjectionKind = "oe" | "msrp" | "retail"
    rows: int                        # 注入行数
    bands_by_kind: dict[InjectionKind, dict[Band, int]]
    held_back_rate: float | None     # 注入行落在黄档或红档的比例
    green_injected: int              # 绿档中的注入行（会被直接使用的错行）
    auroc: float | None              # 未注入且可直接使用的行 vs 注入行

class RunEval(BaseModel):            # schema_version "1.1"（新增 injection）
    run_id: str
    created_at: str
    strategy: StrategyName
    truth_path: str
    rows: int
    reviewed_rows: int               # 按复核前的系统结论评估的行数
    oe: OEEval
    msrp: MSRPEval
    retail: RetailEval
    should_cost: ShouldCostEval
    calibration: CalibrationEval
    usage: UsageEval
    row_evals: list[RowEval]
    injection: InjectionEval | None = None   # 注入实验的运行才有
```

结果库内部的持久化模型（每行四路结果 `RowOutcomes`、收尾后的行 `StoredRow`）只在编排器与结果库之间使用，定义在 `store/runs.py`，不属于跨模块契约。例外：错误注入实验（§14）直接改写结果库里的四路结果，所以 `eval/inject.py` 也使用 `RowOutcomes`。

LLM 产出必须用 structured output 解析成上述模型；校验失败时把错误信息附回 prompt 重试，最多 2 次；仍失败则该行输出 `RowFailure` 记录原因，**不能让整批崩溃**。

---

## 8. LLM 层

- `llm/factory.py`：`get_chat_model(role: str)` 按 `settings.yaml` 返回模型。`role` 用于区分不同 agent 和 judge，可以给 judge 配不同的模型。
- 支持的 provider：`deepseek`（默认）、`anthropic`、`qwen`（OpenAI 兼容接口）、`fake`。
- `llm/fake.py`：**FakeLLM**。它按任务类型注册基于规则的处理函数（例如用正则从页面文本抽 OE 号和价格），返回合法的结构化输出。它的作用是让整条流水线在测试里零成本跑通，**不要求准确率**。FakeLLM 不得读取 ground truth。
- `llm/tracking.py`：每次调用都把 `run_id、agent、模型、输入 token、输出 token、成本、耗时` 写入 `llm_calls` 表。单价写在配置里。
- 并发：全局 asyncio Semaphore，默认最多 8 个并发 LLM 调用。
- 环境变量 `RFQ_LLM_PROVIDER=fake` 时全局强制使用 FakeLLM。

---

## 9. Mock 外部工具（`tools/`）

模拟原系统里的网页检索。每个工具都实现统一接口，以后可以换成真实实现：

```python
class ToolProvider(Protocol):
    name: str
    async def search(self, query: str) -> list[ToolHit]   # ToolHit: page_id, title, text
```

| 工具 | 说明 |
|---|---|
| `oem_catalog_search` | 按件号查 OEM 目录页面 |
| `interchange_search` | 按客户件号查互换候选 |
| `msrp_lookup` | 按 OE 号查厂商价格页 |
| `retail_search_{1..5}` | 5 个零售站点各一个 |
| `external_spec_search` | 查外部产品页面，获取材质、重量 |

工具返回的是**带噪声的文本**，由 LLM 负责抽取和判断，这样 LLM 的工作才有意义。工具支持注入故障（超时、空结果），由配置开关控制，用于测试容错。

---

## 10. MCP server（内部数据库）

`mcp_server/server.py`：用 Python 官方 MCP SDK 实现，stdio 传输，背后是 SQLite。提供以下工具：

| MCP 工具 | 说明 |
|---|---|
| `lookup_our_part` | 客户件号 → 本公司件号 |
| `get_part_spec` | 本公司件号 → 材质、重量、品类、cycle time |
| `get_material_price` | 材料 → 当前 $/kg |
| `list_materials` | 内部库已知的全部材质 key（P5 新增，供外部材质写法归类） |
| `get_cost_params` | 返回 cost_model.yaml 中的费率 |

Agent 通过 `langchain-mcp-adapters` 连接这个 server。另外提供 `rfq mcp serve` 命令，可以单独启动，用 MCP Inspector 调试。

---

## 11. 四个 subagent

每个 agent 都是一个独立的 LangGraph 子图或可调用对象，有单独的 CLI 子命令，例如 `rfq agent oe-interchange --input data/samples/rfq_sample_20.xlsx`，输出 JSONL。

### 11.1 `oe_interchange`

- **输入**：`RFQLine`
- **流程**：
  1. 如果 `our_part_no` 已提供，或通过 MCP `lookup_our_part` 能查到 → 记为已知对应。
  2. 调 `interchange_search` 和 `oem_catalog_search` 获取候选。
  3. LLM 从页面文本中抽取候选 OE 号和替代关系（superseded by / replaces）。
  4. **确定性代码**沿替代关系构建替代链，取链尾作为 `current_oe`；遇到环则报错并标记。
  5. 有多个候选且无法确定时，`status="multiple"`，保留全部候选。
- **输出**：`OEResolution`
- **测试**：替代链构建（含环检测）、件号标准化、一对多场景。

### 11.2 `oe_msrp`

- **输入**：`OEResolution.current_oe`
- **流程**：`msrp_lookup` → LLM 从页面中抽取 MSRP 并判断是否停产 → 输出。如果当前 OE 号查不到，**沿替代链往回**尝试旧号，并在 reasoning 中注明。
- **输出**：`MSRPResult`
- **业务规则**（P3 确认）：
  - OE 解析为 `multiple` 的行不查 MSRP：`status=not_found`、`oe_part_no` 为空，reasoning 注明“OE 未唯一确定，待人工选择”。不替系统猜主号。
  - OE 解析为 `not_found` 的行直接输出 `not_found`；OE 解析失败的行，MSRP 也记为失败（标明是上游失败）。
  - 回退到旧号取得价格时：`oe_part_no` 仍为当前 OE 号，`quote.matched_part_no` 记实际给出价格的旧号，reasoning 写明回退；是否停产只按给出所选价格的那一页判断。
  - 只有价格数字确实以金额形式出现在页面原文中才采用（规则同 §11.3“防编造”）；MSRP ≤ 0 不采用，视为该号无价格，继续沿链回退。
  - 同一号码有多个不同的可采用价格时取较高价，reasoning 标注“价格冲突”，证据保留这些冲突的价格页。未通过原文核对的页面不作为证据，只记录在 trace 中。
  - 只采用美元价格：页面未标币种视为 USD；页面明确为其他币种的价格不采用（视为该号无价格，继续沿链回退），reasoning 注明。不做汇率换算。

### 11.3 `retail_price`

- **输入**：`current_oe` 和替代链上的全部号码
- **流程**：
  1. 并发查询 5 个零售站点。
  2. 每个站点可能返回多条结果，LLM 选出**匹配最精确**的一条，输出理由和 `match_quality`。
  3. 抽取原价、折扣价、计算折扣率和 `effective_price`。
  4. **确定性代码**做离群检测：与中位数相差超过配置阈值（默认 ±40%）的标为 outlier。
  5. `selected` 的规则：在非离群的结果中，优先 exact 匹配，其次取有货的最低 `effective_price`。规则写在一个独立函数里，便于以后调整。
- **输出**：`RetailResult`
- **业务规则**（P4 确认）：
  - 上游处理同 §11.2：OE 为 `multiple` 的行不查零售价（reasoning 注明“OE 未唯一确定，待人工选择”）；OE 为 `not_found` 的行直接输出空结果；OE 解析失败的行记为失败（标明是上游失败）。
  - 分工：LLM 只从页面抽取事实（标题中的件号原文、原价、折扣价、币种、是否有货、价格片段）；“匹配最精确”的判断由确定性代码按件号比对完成。
  - `match_quality`：标题件号与 `current_oe` 写法完全相同为 `exact`；标准化后相同为 `normalized`；挂在替代链旧号下为 `fuzzy`（reasoning 注明所用旧号）。标准化后不等于链上任何号码的结果一律丢弃。
  - 同一站点有多条匹配时依次比较：match_quality（exact > normalized > fuzzy）→ 有货优先 → `effective_price` 最低 → page_id；reasoning 注明匹配条数和所选页面，落选页面记入 trace。
  - 防编造：原价和折扣价都必须以**金额形式**出现在页面原文中，否则整页不采用（记入 trace）。金额形式指：带货币标记（如 `$25.75`、`25.75 USD`、`USD 25.75`、`EUR 60.00`），或是价格行（含 price / MSRP / list / sale / was / now / each 等字样）中独立的数字；件号里的数字、带单位的数字（如 `12,000 miles`）不算。价格必须大于 0，≤ 0 的整页不采用。只采用美元价格，页面未标币种视为 USD，其他币种不采用，不做汇率换算。
  - 页面 page_id 不在搜索结果中、或标题件号没有出现在页面原文中的抽取结果，整页不采用（防编造，记入 trace）。
  - 折扣由代码计算：`discount_pct = (原价 − 折扣价) / 原价 × 100`，四舍五入（ROUND_HALF_UP）保留 1 位小数；折扣价 ≥ 原价时忽略折扣价；只有折扣价时原价和折扣率为空，`effective_price` 取折扣价。
  - 库存：页面写“Ships today”视为有货。页面未写库存状态时 `in_stock` 输出为空（不改成 false）；选价时视为无货，reasoning 注明。
  - 离群检测：比较 `effective_price`，中位数取该行全部报价（含无货、fuzzy）；`|价格 − 中位数| / 中位数 > retail_outlier_threshold` 判为离群；报价少于 3 条时不做检测，reasoning 注明。
  - `selected`：排除离群 → 按 match_quality 分层（exact > normalized > fuzzy），取最高的非空层 → 有货的优先（全部无货则不限）→ `effective_price` 最低 → 站点名。全部报价都被判为离群时不选（`selected` 为空），reasoning 注明“价格全部偏离中位数，无可靠参考价，需人工确认”。
  - 站点搜索失败：同一站点任一号码重试后仍失败，该站点整体视为失败，不出报价，reasoning 注明；5 个站点全部失败时整行记为失败。

### 11.4 `should_cost`

- **输入**：`RFQLine`、`OEResolution`
- **流程**：
  1. 通过 MCP 从内部库取规格（材质、重量）。
  2. 内部库缺失时，调 `external_spec_search`，由 LLM 从页面中抽取。
  3. 两边都有时比较是否一致，结果记在 `spec_source` 中。**不一致时以内部库为准**，外部页面只用来补内部库缺失的字段。
  4. **全部计算都由确定性代码完成**（LLM 不参与算数），公式如下，费率全部读自 `cost_model.yaml`：

```
material      = weight_kg × material_price_per_kg × (1 + scrap_rate)
conversion    = cycle_time_min / 60 × (labor_rate_per_hr + machine_rate_per_hr)
overhead      = conversion × overhead_rate
sga           = (material + conversion + overhead) × sga_rate
profit        = (material + conversion + overhead + sga) × profit_rate
packaging     = 按品类的固定值
FOB           = material + conversion + overhead + sga + profit + packaging

freight       = 按品类的每件海运费
insurance     = (FOB + freight) × insurance_rate
duty          = FOB × (mfn_duty_rate + additional_tariff_rate)   # 美国关税以 FOB 价值为基数
mpf           = FOB × mpf_rate
brokerage     = brokerage_per_entry / units_per_shipment
inland_freight= 按品类的每件内陆运费
landed_cost   = FOB + freight + insurance + duty + mpf + brokerage + inland_freight
```

- `cost_model.yaml` 顶部必须注释：**所有费率均为示意值，不代表任何真实关税或报价。**
- **输出**：`ShouldCostResult`（含完整 `CostBreakdown`）
- **业务规则**（P5 确认）：
  - 以本公司件号为主，OE 状态只影响外部查询：本公司件号已知（询价单给出或对照表查到）→ 经 MCP 取内部规格（品类、材质、重量、cycle time），与 OE 状态无关。外部页面只在 OE 为 `resolved` 时按 `current_oe` 查询（不查替代链旧号）；OE 为 `multiple` 不替系统选主号，`not_found` 无号可查，此时外部视为没有，reasoning 注明原因。OE 解析失败的行记为失败（标明是上游失败）。
  - 本公司件号未知时（原系统做法）：只用外部页面估算。品类取自按 `current_oe` 查到的 OEM 目录页（LLM 抽品类原文，代码映射：含 `thermostat` → thermostat，含 `pump` → water_pump），目录页没有再看询价单 description（同一映射）；仍不确定 → `insufficient_data`。cycle time 用 `cost_model.yaml` 的品类默认值，reasoning 注明。询价单给出或对照表查到的本公司件号在内部库中不存在时，同样按“本公司件号未知”处理，reasoning 注明该件号在内部库中查不到。内部记录存在但品类为空时，品类同样按上面的顺序兜底（OEM 目录页 → 询价单 description），其余字段照常用内部值，reasoning 注明“内部库缺品类”；此时目录页用于补数据，查询失败记为行失败。
  - 只要 OE 为 `resolved` 就查外部规格页：内部完整时用来核对，内部缺字段时用来补。每行最多 1 次 LLM 调用（外部规格页和需要时的目录页一起抽取）。
  - 逐字段（材质、重量）判定：两边都有且一致 → `both_agree`；两边不一致 → `conflict`（用内部值）；只有内部 → `internal`；只有外部 → `external`（用外部值补）；两边都没有 → `missing`。整行 `spec_source`：任一字段 `missing` → `missing`、`status=insufficient_data`；否则取两字段中最弱的一个，强弱为 `both_agree > internal > external > conflict`。
  - 一致的标准：重量换算成 kg 后相对误差 ≤ `pipeline.spec_weight_tolerance`（默认 0.05）；材质 key 相同。
  - 材质归类：LLM 抽取页面上的材质原文，并从内部库已知材质 key（MCP `list_materials`）中选一个，都不像填 `other`；代码核对原文出现在页面中。
  - 防编造（不通过的字段视为外部没有，记入 trace）：页面 page_id 必须在搜索结果中且原文提到 `current_oe`；重量的“数字 + 单位”必须紧挨着出现在原文中，单位只认 kg / g / lb（含 kgs、lbs，大小写不限），换算后 > 0；材质原文、品类原文必须出现在页面中（不区分大小写）。重量换算成 kg 后立即四舍五入（ROUND_HALF_UP）到 3 位小数（精确到克，与内部库精度一致），之后的一致性比较、成本计算和输出都用取整后的值；`breakdown` 各项金额不取整。
  - 同一 `current_oe` 有多个通过核对的外部页面：彼此一致（重量在容差内、材质 key 相同）则合为一个外部值（取 page_id 最小的页的数值，evidence 保留全部页）；彼此不一致则该字段的外部值不可用——内部有值时该字段记 `conflict`、用内部值；内部也没有时该字段 `missing`，reasoning 写“外部页面之间不一致，需人工确认”。目录页之间品类不一致 → 品类未知。
  - 其他计算输入：材质为 `other`、`get_material_price` 查不到或非 USD、品类未知 → `insufficient_data`，`breakdown` 为空，reasoning 写明缺什么；`spec_source` 仍按上面的规则给出。零件没有 cycle time 时用品类默认值。
  - `material` / `weight_kg` 填实际用于计算的值；`insufficient_data` 时已知字段照填，缺的为空。
  - 失败：外部只用于核对时，搜索或抽取失败不算行失败，按内部值估算（`spec_source=internal`），reasoning 注明核对失败；外部用于补数据时，搜索或抽取失败记为行失败（`search_spec` / `extract_spec`），与“数据确实没有”的 `insufficient_data` 区分；MCP 内部库调用出错记为行失败（`internal_spec`）。
- **测试**：公式逐项单元测试；缺规格时 `status="insufficient_data"`。

---

## 12. 实体消解、置信度与输出

### 12.1 实体消解（`resolution/`，纯确定性）

- 处理三类件号：客户件号、OE 号、零售标题件号（零售商品标题上写的件号原文，即 OE 号在零售站点上的写法；原称“零售 SKU”，站点没有独立的库存编号）。Schema 中三者是分开的字段，**不得混用**。
- 合并 `oe_interchange`、`oe_msrp`、`retail_price`、`should_cost` 四路结果，每个 `row_id` 生成一条 `WorksheetRow`。
- 多对一时（多个客户件号对应同一 OE 号），价格结果复用，并在行内注明共享关系。
- 消解的输出是每行一条 `ResolvedRow`（含扁平的 `WorksheetRow` 和完整的四路结果）。
- **业务规则**（P6 确认）：
  - 多对一：OE 为 `resolved` 且 `current_oe` 标准化后相同的行成为一组“共用 OE 组”。组内 MSRP 和零售结果统一用代表行的结果；代表行为组内 row_id 最小、且该路没有行失败的行（两路分别选）。每行注明“与第 X、Y 行共用 OE，价格取自第 X 行”。组内各行自己的价格结论不一致（MSRP 状态或数值、selected 站点或价格不同）时，照用代表行的结果，需复核原因写“共用 OE 的行价格结论不一致”。应有成本不共用（取决于本公司件号）。OE 为 `multiple` 的行不分组。
  - 一对多：OE 为 `multiple` 的行 `current_oe` 为空，`oe_candidates` 列出各候选的当前号（只列原文核对得上的候选，`llm_inferred` 不列，只保留在 `ResolvedRow.oe` 中），价格列为空，需复核原因写“OE 未唯一确定，待人工选择”。
  - OE 不是 `resolved`（multiple / not_found）时，价格 agent 按规则不查价；工作表的价格列和 `msrp_status` 都为空，`ResolvedRow` 保留 agent 的原始结果。
  - 行失败：某路为 `RowFailure` 时该行照常输出，该路的列为空，`failures` 记“路:阶段:原因”，需复核原因写“某路未查成”，与“查了没有”（not_found / insufficient_data）分开表述。OE 失败时下游三路的 `upstream_oe` 失败只报一次根因“OE 解析失败”。
  - 四路结果与询价单的 row_id 集合必须一致，否则报错，不输出部分结果。
  - 各阶段 reasoning 摘要：每路取前 200 字符；该路行失败时为失败原因。
- `WorksheetRow` 的列：row_id、客户件号（原始/标准化）、本公司件号、current_oe、替代链、OE 状态、MSRP、零售各站点 effective_price、selected 零售价及来源、零售价离群标记、should-cost FOB、landed cost、建议参考价区间（MSRP、零售价、landed cost 三者并列展示，不由系统给出最终报价）、置信度分数、档位、需复核原因、各阶段 reasoning 摘要。

### 12.2 置信度（`confidence/`）

做成**可切换的策略**，由 `settings.yaml` 的 `confidence.strategy` 选择（默认 `evidence_plus_judge`，P10 错误注入实验后确认：只有它能把看起来合理的错行大量压出绿档；每千行约多 $1.65）：

| 策略 | 说明 |
|---|---|
| `evidence` | 只用确定性证据信号加权：OE 匹配类型、替代链是否完整、找到的价格来源数、零售价离散程度、MSRP 与零售价是否合理（零售价通常不应远高于 MSRP）、规格来源（both_agree > internal > external > conflict）。权重写在配置里。 |
| `evidence_plus_self` | 在 `evidence` 基础上，让 LLM 对关键判断（OE 匹配、零售价选择、MSRP）给出 0–100 自评，并**采样两次**（temperature 0.7），两次答案一致性和自评分数合成 LLM 信号，再与证据信号加权。 |
| `evidence_plus_judge` | 在 `evidence` 基础上，对封顶后证据分落在 `judge_band` 的行（默认 50–100，即封顶后证据分 ≥ 50）调用一个独立的 judge 模型复核，judge 看到证据和结论，输出同意/不同意和 0–100 分；judge 只能降分（不同意时压到红档），同意时不加分。 |

- 档位：绿 ≥80（可直接用）、黄 50–79（抽检）、红 <50（必须人工复核）。阈值写在配置里，**最终值由 §14 评估结果决定**。
- 每行输出 `signals` 和 `explanation`，Dashboard 要能展示"为什么是这个分数"。
- **业务规则**（P6 确认）：
  - 分数含义：一行一个分数，表示这一行的结论整体可以直接使用的把握。“行正确”的定义（§14 整行准确率照此计算）：OE 正确（resolved 且等于主号，或 multiple 且候选覆盖可接受集合，或 not_found 且预期也是 not_found）；MSRP 的 status 正确，有价时相对误差 ≤1%；零售 selected 站点正确；应有成本的 status 和规格（品类、材质、重量）正确。任一阶段失败或任一项不对，整行算错。
  - “可直接使用”（P9 后确认，§14 置信度校准与覆盖率-准确率曲线照此计算，与分数含义一致）：行正确，且 OE 为 `resolved`，且零售不是“有报价但全部离群”。即 OE 为 `not_found` / `multiple` 的行即使结论正确也不算可直接使用；应有成本 `insufficient_data` 的行仍可算（MSRP 与参考零售价可用）。
  - `evidence` 的六个信号（0–1）：
    - `oe_match`：候选中最好的匹配类型，exact 1.0、normalized 0.9、interchange 0.7、llm_inferred 0.2。
    - `chain`：替代链被截断（`chain_truncated`）0.3，否则 1.0。
    - `price_sources`：n = MSRP 为 found / discontinued 记 1，加上非离群零售报价数；信号 = min(n, 4) / 4。
    - `retail_dispersion`：非离群报价 `effective_price` 的变异系数 cv（总体标准差 / 均值）；信号 = max(0, 1 − cv / `retail_outlier_threshold`)；少于 2 条记 0.5。
    - `msrp_retail`：r = 参考零售价 / MSRP；r ≤ 1.0 记 1.0，r ≤ `msrp_retail_suspicious_ratio`（默认 1.2）记 0.5，超过记 0；任一缺失记 0.5。
    - `spec_source`：both_agree 1.0、internal 0.8、external 0.5、conflict 0.3、missing 0。
    - 证据分 = ROUND_HALF_UP(100 × Σ(权重 × 信号) / Σ权重)。权重和各档取值都写在配置里（§18）。
  - 封顶（三种策略都适用，最后执行，LLM 不能把封顶的行抬上去）：
    - 封顶到红档上限（`yellow_min − 1`）：OE 为 `multiple`；OE 为 `not_found`；任一路行失败；零售有报价但全部离群（一条报价都没有不封顶，由信号压低分数）。
    - 封顶到黄档上限（`green_min − 1`）：共用 OE 的行价格结论不一致；应有成本 `insufficient_data`。
    - 触发的封顶写进 `signals`（`gate_*`）和 explanation。
  - 混合分一律用**封顶后的证据分**（与选择 judge 行的口径一致），混合后再封顶一次。
  - `evidence_plus_self`：
    - 只对没有红档封顶的行调用（证据分本身低于 50 但没有红档封顶的行也调用）。角色 `self_eval`，模型同主模型，temperature 0.7。
    - 同一 prompt 采样 `self_samples` 次。LLM 自己回答关键判断并给出把握（0–100）：它认为正确的当前 OE 号；有参考零售价时，再回答它会选的站点；有 MSRP 报价时，再回答它从 MSRP 证据片段读出的 MSRP（美元数字，P10 追加，prompt 写明 §11.2 的只用美元、同一号码取较高价、可用替代链旧号的价格）。自评摘要的 MSRP 一节只给证据（来源、页面号、原文片段）与所查号码，不给系统抽出的价格和状态。
    - 每个判断的信号 = 0.5 × score + 0.5 × consistency，其中：score = 各次采样（答案与系统结论一致时为把握 / 100，否则为 0）的平均；consistency = 各次答案（件号标准化后比较）全部相同时为 1，否则为 0。MSRP 题的“一致”按相对误差 ≤1% 判断：与系统 MSRP 相差 ≤1%（以系统 MSRP 为分母）即与系统一致；各次答案都给出、且与第一次答案相差 ≤1%（以第一次答案为分母）即 consistency 为 1。判对口径与 §14 的 MSRP 判对共用同一个函数。
    - LLM 信号 = 各判断信号的平均。最终分 = ROUND_HALF_UP((1 − `self_weight`) × 封顶后证据分 + `self_weight` × 100 × LLM 信号)，然后封顶。
    - 部分采样失败时只用成功的采样；全部失败则用证据分，explanation 注明。都不算行失败。
  - `evidence_plus_judge`：
    - 只对证据分（封顶后）落在 `judge_band` 的行调用 `judge` 角色，每行 1 次。
    - judge 看到整行的证据和结论（不含 ground truth），输出 agree、score（0–100）、concerns。
    - judge 的 prompt 写明系统有意执行的业务规则（P9 后确认）：规格内外部冲突时以内部库为准；MSRP 同一号码多个价格取较高价、可沿替代链回退旧号、只采用美元；零售离群为偏离中位数超过 `retail_outlier_threshold`，参考零售价按匹配质量 → 有货 → 实际价最低选；OE 为 `multiple` 的行不查价。并说明这些规则不作为疑点，judge 只检查证据是否读对、规则是否用对。规则文本在代码中只有一份。
    - judge 同意时最终分 = 封顶后证据分（不加分，P10 注入实验后确认：同意时加分会把 judge 没看出来的错行抬上绿档），concerns（如有）也写进需复核原因，作为抽检提示（P9 后确认）；不同意时混合分 = ROUND_HALF_UP((1 − `judge_weight`) × 封顶后证据分 + `judge_weight` × score)，再封顶到红档，且不高于封顶后证据分（judge 只能降分，任何 `judge_band` 配置下都成立），concerns 写进需复核原因。
    - judge 调用失败时用证据分，explanation 注明。不算行失败。
    - 需复核原因中 judge 的写法（P10）：不同意时 `judge: <concerns>`（concerns 为空时写 “disagrees with the conclusions”）；同意且 concerns 不为空时 `judge note: <concerns>`，为空不写。
  - 需复核原因：所有档位都填，包括封顶原因、各路失败、MSRP 未找到 / 停产 / 回退旧号、无参考零售价、离群站点、规格冲突 / 缺失 / 数据不足、共用 OE 价格不一致、judge 的 concerns（同意与不同意时都写）。explanation 写清分数构成：封顶前的证据分和触发的封顶、得分最低的信号、LLM 部分（写明参与混合的封顶后分数），以及混合结果是否再次被封顶。

### 12.3 输出

- Excel 定价工作表：一行一个询价件号，按档位给整行着色（绿/黄/红），冻结表头，第二个 sheet 是本次运行汇总（行数、各档位数量、覆盖率、LLM 成本、耗时）。
- 同时写入 SQLite 结果表，供 API 读取。
- **细则**（P8 确认）：
  - 工作表由结果表 `run_rows` 生成：API 每次下载现场生成（反映最新复核），CLI 收尾后写 `run_<run_id>.xlsx`。
  - 列用英文字段名（与 API 同名），顺序：row_id、客户件号（原始 / 标准化）、本公司件号、OE 状态、current_oe、替代链、OE 候选；参考价区间三列 MSRP、参考零售价、landed cost 并列；然后 MSRP 状态、各站点实际价（每站点一列）、参考零售价来源、离群站点、should-cost FOB、共用 OE 行、价格来源行、共用价格不一致、置信度分数、档位、复核状态、需复核原因、行失败、四路 reasoning 摘要各一列。列表用逗号（替代链用 ` -> `，需复核原因与行失败用换行）连接。
  - 着色：绿 `C6EFCE`、黄 `FFEB9C`、红 `FFC7CE`，整行填色；复核不改变颜色（档位不变，复核状态在本列显示）。冻结表头与 row_id 列，表头加自动筛选。
  - 汇总（`RunSummary`，与 `GET /runs/{id}` 相同）：覆盖率按系统结果计算——OE resolved、MSRP found 或 discontinued、有参考零售价、应有成本 estimated，各占总行数的比例，不含人工改价；耗时为 `finished_at − created_at` 的墙钟时间（含被 kill 后的停机时间）。

---

## 13. Orchestrator（LangGraph 主图）

- **运行（run）**：一次 `rfq run` 对一份询价单的完整处理，有唯一 `run_id`。建运行时 intake 一次，询价行写入结果库，之后 resume / 重跑不再读 Excel。运行参数（数据集、批大小、置信度策略、是否复核）记在 `runs` 表，resume / rerun 沿用。
- **两类图**（都用 LangGraph 的 SQLite checkpointer，检查点为单独文件，默认 `data/checkpoints.db`）：
  - **批图**，`thread_id = f"{run_id}:{batch_id}"`。State：`run_id`、`batch_id`、当前批的 `RFQLine` 列表、各阶段结果字典（按 row_id 索引）、错误列表。节点：`load_batch → oe_interchange（Send 按行）→ plan_prices → [oe_msrp ∥ retail_price ∥ should_cost]（Send 按行）→ collect → persist_batch`。
  - **收尾图**，`thread_id = f"{run_id}:final"`，全部批结束后对全表运行：`resolve（实体消解）→ confidence（Send 按块）→ (review) → finish`。实体消解和置信度在收尾阶段做，是因为多对一的共用 OE 组通常跨批（§12.1 的规则要在全表范围内成立）。
  - 四个 agent 子图作为普通函数调用（不继承检查点），检查点粒度是“一行一个 agent”。
- **分批**：外层按 `batch_size`（默认 100）、按 row_id 顺序切分，批之间顺序执行。每批四路结果在一个事务里写入结果库，`batch_results` 标记 `completed`。批内行级失败不影响其他行；行级容错兜不住的异常（如 MCP 起不来、写库出错）使整批标记 `failed`，不落结果，后面的批照常进行。收尾时失败批的每行四路都记为行失败（stage `batch`），照常进入工作表。
- **多对一的价格复用**（P7 确认）：只对 OE 为 `resolved` 的行，按标准化后的 `current_oe` 去重，同一 `current_oe` 在整个运行中只跑一次 `oe_msrp` 和 `retail_price`：
  - 先查结果库中已保存的该 `current_oe` 的结果，该路不是行失败就直接复用（MSRP、零售分开判断）；行失败的结果不复用；本次正在重跑的行自身的旧结果不复用（重跑的行四路全部重跑）；只复用某行自己跑出来的结果，不复用多对一复制给其他行的副本。
  - 批内第一次遇到的行（row_id 最小）为领头行，用它的 `OEResolution` 跑价格 agent；同批其他行复制结果（只改 `row_id`），领头行失败时同批组员该路记同一失败。
  - 应有成本不去重（按本公司件号）。因组内只查一次价，“共用 OE 的行价格结论不一致”在编排运行中不会出现。
- **重新打分（rescore，P10）**：`rfq rescore RUN_ID --strategy S [--allow-real]` 新建一个运行，复制源运行的询价行、批与四路结果（批状态原样保留：完成的批为 `completed`，失败的批仍为 `failed`，收尾时其行按批失败记为行失败），只做收尾（消解 + 置信度，不开复核），四个 agent 不重跑。`RunSpec` 沿用源运行（`strategy` 换成 S、`review=False`），记 `rescored_from`。源运行必须已收尾、且没有人工复核记录。运行前打印 judge / self 的准确调用数（源运行的证据分已知）。用于在同一份 agent 结果上比较置信度策略（§14）。
- **错误注入（P10 追加实验）**：`rfq eval inject SOURCE [--rows 40] [--seed 7]` 按重新打分的方式复制源运行（要求同上），在可注入行上改写四路结果后用 `evidence` 收尾（不调 LLM），注入了哪些行记在结果库 `run_injections`；复制、改写与记录在同一个事务里完成；之后对它 `rfq rescore`，注入记录随运行一起复制。规则见 §14。
- **检查点与 resume**：批是否完成以结果库为准，`completed` 的批直接跳过；未完成的批若有检查点（进程被 kill），用同一 `thread_id` 以空输入恢复，只重跑未完成的行级任务；没有检查点的批从头跑。`failed` 的批 resume 时不重试。收尾图同理，有挂起的复核就继续复核。
- **CLI**：
  - `rfq run --input X.xlsx [--review] [--strategy] [--batch-size] [--limit] [--dataset] [--allow-real]`：新建运行，返回 run_id
  - `rfq run --resume RUN_ID`：跳过已完成的批，从断点继续
  - `rfq rerun-failed RUN_ID`：重跑失败的批，以及已完成批中任一路行失败的行（整行四路重跑，新结果替换旧结果），然后重做收尾。开始时先清掉这些批 thread 和收尾 thread 的旧检查点（包括复核暂停时挂起的复核），再在结果库记下重跑计划（运行改回 running，每个要重跑的批改回 pending 并记下要重跑的行），然后各批以新输入开始；中途被 kill 后 `--resume` 按记下的计划把剩下的批跑完，并从头重做收尾（复核队列按重跑后的结果重新生成，只排还没复核过的红档行，已复核的决定保留）
  - `rfq run --input X.xlsx --review`：开启人工复核
  - `rfq runs show RUN_ID`：各批状态与执行次数、档位分布、LLM 调用数与成本
  - 运行前按角色打印 provider / model 和预计 LLM 调用数；真实 provider 必须加 `--allow-real`（resume、rerun-failed 同样）。
  - 收尾完成后（状态为 `awaiting_review` 或 `completed`）导出 `data/runs/run_<run_id>.jsonl`（每行一条 `ResolvedRow`）和定价工作表 `run_<run_id>.xlsx`（§12.3）。
- **人工复核（`--review`，P8 修订）**：只有建运行时开了复核（`RunSpec.review`）的运行能复核。**先收尾完成、工作表可下载，复核在已完成的运行上进行**：收尾后仍有红档、未复核的行时运行状态为 `awaiting_review`，队列为空时为 `completed`；`review=False` 的运行收尾后直接 `completed`。复核节点用一次 `interrupt()` 交出整个待复核队列（`ReviewQueue`，按 row_id）；恢复值是对队列中某一行的决定（`ReviewDecision.row_id`），不在队列中的决定被忽略。每条决定单独进检查点，记录后回到 `resolve` 重新汇总（指纹没变的行沿用原分数），再用剩余队列重新挂起；复核中新变红的行继续入队。CLI 每次取队首一行展示证据和结论，用法不变；API 可以答队列中的任意一行（§15.1）。复核记录写入 `review_log` 表（动作、修改前后的 OE / MSRP / 参考零售价、修改前整行快照），结论写入 `WorksheetRow.review_status`。业务规则（P7 确认）：
  - **接受**：只记复核状态 `accepted`。
  - **拒绝**：行保留在工作表上，数值和档位不变，复核状态 `rejected`。
  - **修改 OE 号**：用输入的号构造 `OEResolution`（`resolved`，替代链只有该号，本公司件号沿用原结果，候选 `match_type="manual"`，证据来源 `manual_review`），对该行重跑 `oe_msrp`、`retail_price`、`should_cost`；复核结束后全表重新消解（共用组可能变化），结果有变化的行重算置信度。复核状态 `modified`，不再进入复核队列；之后的 `rerun-failed` 对该行不重跑 OE。
  - **修改价格**：只覆盖工作表上的 MSRP 和 / 或参考零售价（USD，> 0）：`msrp_status` 记 `found`，`selected_retail_source` 记 `manual_review`，需复核原因追加“价格为人工输入”；各 agent 的原始结果不变，不重跑 agent、不重算置信度。复核状态 `modified`。人工价格只叠加在工作表上显示：该行之后因其他原因（如 rerun-failed）重新打分时，置信度（包括 judge 看到的内容）仍按系统自己的结论计算，不使用人工价格（P7 review 后确认）。
- **运行租约**（P8）：同一时刻只能有一个进程驱动一个运行。`runs` 表记 `lease_owner`（进程 token）和 `lease_heartbeat`；开始驱动时用一条 UPDATE 原子抢占（owner 为空、是自己、或心跳超过 `storage.lease_ttl_s`，默认 60 秒），持有期间每 `storage.lease_heartbeat_s`（默认 10 秒）刷新心跳，刷新时发现租约已被别人接手就停止驱动，结束时释放。同一主机上持有租约的进程已经不存在（被 kill）时立即接手，不等心跳过期；API 交给 worker 的租约由 worker 换成自己的进程标识。抢不到时明确报错（写明占用者与距上次心跳的秒数）。取租约的操作：`rfq run`（新建与 `--resume`）、`rerun-failed`、API 启动的 worker、API 提交复核；只读查询不取。
- **后台 worker**（P8）：API 建运行后启动独立子进程执行 `rfq run --resume RUN_ID`（与 API 进程脱离，API 重启不影响），API 先取得租约再交给 worker。被 kill 的 worker 租约过期后，可用 `POST /runs/{id}/resume` 或 CLI `--resume` 续跑。
- **SQLite 多进程**（P8）：结果库与检查点库都开 `journal_mode=WAL` 并设 `busy_timeout`。
- **验收**：用 8000 行数据运行，中途手动 kill 进程，`--resume` 后已完成的批不重复执行，最终结果与一次跑完的结果一致。

---

## 14. 评估脚本（`eval/`）

`rfq eval --run RUN_ID` 将运行结果与 `ground_truth` 对比，生成 `reports/eval_{run_id}.md` 和图表 PNG。

| 指标 | 说明 |
|---|---|
| OE 解析 | 覆盖率（resolved 占比）、准确率（current_oe 正确占比）、替代链完全正确占比 |
| MSRP | 覆盖率、误差（相对误差 ≤1% 视为正确） |
| 零售价 | 每站点覆盖率、selected 是否正确、离群检测的精确率和召回率 |
| Should-cost | 规格抽取正确率；成本估算相对 ground truth 的 MAPE |
| 置信度校准 | 以“可直接使用”（§12.2）为标签：各档位的实际比例、ECE、AUROC、可靠性图（reliability diagram）；另报红档中“行正确”的比例（被正确拦下的行） |
| 覆盖率-准确率曲线 | 只自动处理分数 ≥ 阈值的行时，覆盖率与准确率（按“可直接使用”）随阈值的变化，用来选阈值 |
| 成本与耗时 | 每个 agent 的调用次数、token、成本、总耗时、每千行成本 |

`rfq eval compare --runs A B C`：对比不同置信度策略或不同模型的运行，输出一张对比表。这是选择置信度策略和阈值的依据。

细则（P10 确认）：
- `rfq eval --run RUN_ID [--truth PATH] [--out reports]`：只评估已收尾的运行（`awaiting_review` / `completed`）。ground truth 自动推断：去掉 API 上传文件名的 `<uuid>_` 前缀后，`rfq_sample_*` 用 `<dataset>/<stem>_ground_truth.jsonl`；数据集目录中有同名询价单的（数据集自己的询价单，或它的上传副本）用 `<dataset>/ground_truth.jsonl`；路径按项目根目录解析后比较；推断不出时报错并提示 `--truth`。运行的每一行都必须在 truth 中、标准化后的客户件号相同，否则报错；truth 中多出的行（`--limit` 的运行）不参与评估。
- 评估系统自己的结论：复核过的行取 `review_log` 中最早的快照（复核前），人工价格不参与。批失败的行按行失败算错。
- 结果为 `RunEval`（§7），存入结果库 `run_evals` 表（重跑覆盖），并写 `reports/eval_{run_id}.md`、`eval_{run_id}_reliability.png`、`eval_{run_id}_coverage.png`（matplotlib）。评估不调用 LLM。
- 指标口径：
  - OE：覆盖率 = resolved / 全部行；准确率 = resolved 行中 current_oe 等于主号；另报全行 OE 结论正确率；替代链完全正确 = 两边都 resolved 的行中链（标准化后）相同。
  - MSRP：覆盖率 = found 或 discontinued / 全部行；准确率 = 有价的行中相对误差 ≤1%；另报 status 正确率与平均相对误差。
  - 零售：每站点覆盖率 = 拿到报价的行 / truth 中 OE 为 resolved 且在该站点有挂牌的行；参考零售价站点正确率按全部行；离群检测按（行，站点）对算精确率、召回率，只看 truth 为 resolved 的行，没有预测离群时精确率为空。
  - 应有成本：规格正确率；到岸成本 MAPE 只算两边都 estimated 的行，按零件自身 / 品类默认 cycle time 分开报。
  - 置信度校准：概率 = score / 100，标签“可直接使用”。ECE 用 10 个等宽区间（[0,10) … [90,100]），Σ(区间行数 / 总行数) × |平均分数 − 实际比例|。AUROC 按 Mann-Whitney 秩和（平局取平均秩），只有一类标签时为空。
  - 覆盖率-准确率曲线：阈值 0–100 每个整数；另列目标准确率 90% / 95% / 99% 可用的最低阈值及其覆盖率；曲线图（报告 PNG 与 Dashboard）在 95% 的最低阈值处画竖线标注。评估只给出依据，档位阈值与权重的调整由用户决定后改配置。
  - 成本与耗时：`llm_calls` 按 agent 分组；每千行成本 = 合计成本 / 行数 × 1000；复制来的运行（rescore、注入）的合计含复制链上所有源运行的调用（沿 `rescored_from` 一直往上加）。
- `rfq eval compare --runs A B C [--out reports]`：只读已存的评估，缺评估报错；各运行必须用同一个 truth 文件、评估同一组行（同一份询价单），否则报错。输出 `reports/compare_<时间>.md`、叠加的可靠性图与覆盖率-准确率曲线 PNG，终端打印对比表。列：运行、策略、模型、行数、绿 / 黄 / 红、行正确率、可直接使用率、ECE、AUROC、绿档可直接使用比例、红档行正确比例、目标准确率 95% 的阈值与覆盖率、本运行 LLM 调用与成本、合计成本、每千行成本、耗时。
- 三种策略的对比：先用 `evidence` 跑一次完整运行，再用 `rfq rescore` 在同一份 agent 结果上生成另外两种策略的运行（§13）。
- 错误注入实验（P10 追加，合成数据上各阶段全对、分不出策略时用）：
  - 可注入行（只看系统结果）：没有红档封顶、OE 为 resolved、没有行失败、不在共用 OE 组、有 MSRP 报价、有参考零售价且还有别的非离群报价。
  - 按 seed 打乱可注入行、取前 N 行（不足报错），第 i 行的类型依次为 OE / MSRP / 零售：OE 错 = `current_oe` 与替代链链尾换成相邻假件号（最后一位数字 +1，9 变 0）；MSRP 错 = MSRP 报价 × 1.15–1.35；零售错 = 参考零售价改为另一个非离群站点。候选、证据片段和其他路都不改；ground truth 不变，注入行按 §12.2 判为行错。
  - 评估多报 `RunEval.injection`：按类型的各档行数、被拦下的比例（落在黄档或红档）、绿档中的注入行、AUROC（正类 = 未注入且可直接使用的行，负类 = 注入行）。报告、`eval compare`（新增三列，没有注入的运行为 `-`）与 Dashboard 评估页都显示。

---

## 15. API 与 Dashboard

### 15.1 FastAPI（`api/`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/runs` | 上传 Excel，后台启动运行，返回 run_id |
| GET | `/runs` | 运行列表 |
| GET | `/runs/{run_id}` | 运行状态、进度（已完成批数/总批数）、汇总指标 |
| GET | `/runs/{run_id}/rows` | 行列表，支持 `band`、`status` 过滤和分页 |
| GET | `/runs/{run_id}/rows/{row_id}` | 单行完整详情（含全部证据和 reasoning） |
| POST | `/runs/{run_id}/rows/{row_id}/review` | 提交人工复核结果 |
| GET | `/runs/{run_id}/worksheet.xlsx` | 下载定价工作表 |
| GET | `/runs/{run_id}/eval` | 评估结果（如果有） |
| POST | `/runs/{run_id}/resume` | （P8 补充）续跑被中断的运行：租约空闲或过期时启动 worker |

响应全部使用 §7 的 Pydantic 模型，自动生成 OpenAPI 文档。以后的 React 前端只依赖这套接口。

细则（P8 确认）：
- 启动：`rfq api serve [--host] [--port] [--allow-real]`。没有 `--allow-real` 而配置是真实 provider 时，会触发 LLM 调用的请求（建运行、续跑、复核改 OE 号）返回 409 并给出预计调用数。
- `POST /runs`：multipart，`.xlsx` 文件 + 可选 `strategy`、`batch_size`、`review`、`limit`、`dataset`；服务端 intake（失败 422），建运行后启动 worker（§13），返回 202 与 `RunRecord`。
- `GET /runs`：`list[RunRecord]`，新的在前。`GET /runs/{id}`：`RunDetail`（进度读 `batch_results`）。
- `GET /runs/{id}/rows`：`RowPage`；`band`、`status`（复核状态 `unreviewed / accepted / modified / rejected`）可重复传；`offset` 默认 0，`limit` 默认 100、最大 1000，按 row_id 排序。`GET .../rows/{row_id}`：`RowDetail`。
- `POST .../review`：请求体 `ReviewInput`。运行未开复核或不在 `awaiting_review`、该行不在当前待复核队列、或运行正被其他进程驱动时返回 409。接受 / 拒绝 / 改价同步完成，返回 200 与 `ReviewOutcome`（`done=true`）；改 OE 号要重跑下游三路，返回 202（`done=false`），由后台 worker 完成。处理改 OE 号的 worker 中途被 kill 时，运行仍是 `awaiting_review`、该行仍在队列中：重新提交这一行的同一决定即可完成（`POST /resume` 只用于状态为 `running` 的运行）。
- `GET .../worksheet.xlsx`：运行未收尾时 409。
- `GET .../eval`：返回已存的 `RunEval`（§14，由 `rfq eval` 生成）；还没有评估时 404。API 不触发评估。
- 未知运行或行返回 404。

### 15.2 Streamlit Dashboard（`dashboard/app.py`）

只通过 HTTP 调 FastAPI，不 import `rfq_agent` 的任何模块（包括 schemas），由测试检查。页面：

1. **上传与运行**：上传询价单，显示进度条。
2. **运行总览**：总行数、绿/黄/红数量、各阶段覆盖率、LLM 成本、耗时。
3. **工作表**：可筛选的表格，按档位着色。
4. **行详情**：替代链可视化、各站点价格对比（标出离群和选中项）、成本瀑布图（material → … → landed cost）、置信度信号分解、各阶段 LLM reasoning。
5. **复核队列**：红档行列表，可在页面上接受、修改、拒绝。
6. **评估**：展示 §14 的指标和图表，包括不同策略的对比。

细则（P9 确认）：
- 启动：`uv run streamlit run dashboard/app.py`；API 地址取环境变量 `RFQ_API_URL`（默认 `http://127.0.0.1:8000`），不读后端配置。Streamlit 在依赖组 `dashboard` 中。图表用 Altair。
- 侧栏选运行（`GET /runs`），选中的运行同步到 URL 参数 `?run=`；上传后自动选中新运行。
- 上传：可选 strategy、batch_size、review（页面默认勾选）、limit；不开放 dataset。422 / 409 显示服务端原文，409（真实 provider）提示服务端需加 `--allow-real`。
- 进度：每 2 秒轮询 `GET /runs/{id}`，直到状态不是 `running`。进度 =（完成 + 失败批）/ 总批数；批都结束但仍为 `running` 显示“收尾中”；`awaiting_review` 显示“已收尾，工作表可下载，还有 N 个红档行待复核”；`running` 且 `lease_active=false` 显示“后台已停止，可续跑”，并提供“续跑”按钮（`POST /resume`）。
- 总览：`RunSummary` 全部指标与运行参数；收尾前没有汇总，只显示进度。
- 工作表：band、复核状态筛选与分页都由 API 完成（每页 50 / 100 / 500 行）；整行按档位着色，颜色与 Excel 相同；可选中一行进入行详情；运行收尾后可下载 xlsx。
- 行详情：结论与参考价区间、替代链与全部 OE 候选、各站点价格对比（标出选中、离群和 MSRP）、成本瀑布图（material → … → FOB → … → landed cost）、置信度信号与封顶（只展示信号值，权重在服务端配置）、四路完整 reasoning 与证据、复核历史。
- 复核队列：只在开了复核且状态为 `awaiting_review` 的运行上可操作，列出红档未复核行（`band=red&status=unreviewed`）。接受 / 拒绝 / 改价返回 200 后刷新；改 OE 号返回 202 后每 2 秒轮询该行，`review_status` 变为 `modified` 即完成，最多 120 秒；期间 worker 停止（`lease_active=false`）则提示重新提交同一决定。
- 评估（P10）：404 显示“暂无评估”并提示 `rfq eval --run <id>`；有评估时展示各阶段指标表、各档表、ECE、AUROC、可靠性图（含对角参考线）、覆盖率-准确率曲线（标出目标准确率的阈值）、成本与耗时；策略对比：多选其他运行，逐个取评估（404 标“未评估”），在页面上拼对比表和叠加曲线（对比表含注入的三列）；运行有注入记录时另显示“注入错误”一节：被拦下的比例、留在绿档的注入行、AUROC 与按错误类型的各档行数。
- 不缓存 API 结果（复核会改数据）；API 错误统一显示服务端的 detail。

---

## 16. 文档

- `README.md`（英文）/ `README.zh-CN.md`（中文）：项目简介、架构图、快速开始（用 fake 模式 3 条命令跑通）、配置说明、评估结果摘要。
  - 快速开始（P11 确认）：`uv sync` → `uv run rfq data generate` → `RFQ_LLM_PROVIDER=fake uv run rfq run --input data/samples/rfq_sample_20.xlsx`，不需要 `.env`；三条命令放在 `<!-- quickstart:begin -->` / `<!-- quickstart:end -->` 之间，由测试在全新副本中执行验收（退出码、产物、跑完后工作区干净）。
  - 中英文分开（P11 review 后确认）：`README.md` 为英文版（GitHub 默认显示），`README.zh-CN.md` 为中文版，两版顶部互相链接、内容一致，快速开始命令由测试检查相同。简介不写作者个人背景，只写目前是学习与交流用的项目、后续计划发展为对接真实企业数据、有意合作可通过 GitHub Issue 联系。
  - 另有“更多用法”（评估、rescore 与策略对比、错误注入、复核、resume / rerun-failed、单个 agent、API、Dashboard、MCP）与“开发”两节。README 中的命令与输出都是 fake 模式实际跑过的；真实 provider 只演示不加 `--allow-real` 时被拦下。评估摘要只用 `docs/plans/` 已记录的真实数字。
- `docs/design-notes.md`：**只生成模板，内容由我本人填写**，不要替我编造原系统的细节。模板章节：
  1. 原系统概述（平台版本）
  2. 为什么拆成四个 agent
  3. 阶段契约的设计
  4. 实体消解的难点
  5. 分批与容错
  6. 置信度策略的选择与评估结果
  7. 平台方案 vs 自建方案的取舍（开发速度、可控性、可观测性、成本、可测试性、厂商锁定）
  8. 如果上生产还需要什么

---

## 17. 实施阶段与验收标准

| Phase | 内容 | 验收标准 |
|---|---|---|
| P0 | 项目骨架、配置加载、LLM factory、FakeLLM、调用追踪、CLI 框架 | `uv run pytest` 通过；`rfq --help` 可用；FakeLLM 能返回结构化输出 |
| P1 | 合成数据生成器、ground truth、SQLite 内部库、MCP server、mock 工具 | 生成 200 行数据，各难点比例符合 §6.3（误差 ±3%）；MCP Inspector 能调用全部工具 |
| P2 | `oe_interchange` agent | fake 模式测试通过；真实 LLM 跑 20 行，输出 JSONL，我人工检查 |
| P3 | `oe_msrp` agent | 同上 |
| P4 | `retail_price` agent | 同上；离群检测单元测试通过 |
| P5 | `should_cost` agent | 同上；公式逐项单元测试通过 |
| P6 | 实体消解 + 置信度三种策略 | 多对一、一对多场景测试通过；三种策略都能切换运行 |
| P7 | Orchestrator：主图、并行、分批、检查点、resume、rerun-failed、人工复核 | §13 的 kill/resume 验收通过；`--review` 模式可交互 |
| P8 | Excel 输出 + FastAPI | 工作表着色正确；全部接口有测试 |
| P9 | Streamlit Dashboard | 六个页面可用，只依赖 API |
| P10 | 评估脚本 + 策略对比 | 生成 eval 报告和图表；用真实 LLM 对 200 行跑三种策略并输出对比表（运行前先报预计成本） |
| P11 | README + design-notes 模板 | 新环境按 README 三条命令能跑通 fake 模式 |

开发顺序说明：P2 在前，因为价格 agent 依赖 OE 解析结果；`should_cost` 不依赖价格 agent，如有需要可以和 P3/P4 调换顺序。

---

## 18. 配置示例

`config/settings.yaml`：

```yaml
llm:
  provider: deepseek            # deepseek | anthropic | qwen | fake
  base_url: https://api.deepseek.com
  model: <实施时查官方文档确认>
  temperature: 0
  max_concurrency: 8
  pricing_per_million:          # 用于成本统计，实施时按官方价格填写
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
  spec_weight_tolerance: 0.05     # should_cost 内外部重量一致的相对误差（P5）

confidence:
  strategy: evidence_plus_judge # evidence | evidence_plus_self | evidence_plus_judge（P10 错误注入实验后改为默认）
  bands:
    green_min: 80
    yellow_min: 50
  self_samples: 2
  self_weight: 0.3              # P6：LLM 自评信号在最终分中的权重
  judge_band: [50, 100]         # P10 追加：judge 也审绿档（只能降分）
  judge_weight: 0.5             # P6：judge 分数在最终分中的权重
  evidence:                     # P6：证据信号的权重与各档取值（§12.2），P10 按评估结果调
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

`llm.roles` 另有 `self_eval`（P6）：模型同主模型，temperature 0.7。

```yaml
storage:                          # P7
  db_path: data/rfq.db            # 结果库（业务表）
  checkpoint_path: data/checkpoints.db   # LangGraph 检查点，单独文件
  upload_dir: data/uploads        # P8：API 上传的询价单
  lease_ttl_s: 60                 # P8：运行租约心跳超过此秒数视为过期
  lease_heartbeat_s: 10           # P8：持有租约时刷新心跳的间隔；lease_ttl_s 必须大于它的 2 倍
```

运行环境要求 SQLite ≥ 3.51.2（P8）：3.51.0 / 3.51.1 在同一进程中一个线程打开数据库、另一个线程关闭连接时可能死锁。代码对经 `store.db` 打开的连接做了串行化保护，并在检测到这两个版本时给出警告。

`config/cost_model.yaml`：

```yaml
# 所有费率均为示意值，不代表任何真实关税或报价
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

（P1 阶段由 Claude Code 填入一组合理的示意数值，并和合成数据的 ground truth 使用同一套参数。）
