# RFQ-Agent

[中文](GLOSSARY.md) | English

> This is a translation. The Chinese [GLOSSARY.md](GLOSSARY.md) is authoritative; if the two differ, the Chinese version wins. Each term gives the Chinese original in parentheses; the _Avoid_ lines list Chinese wordings to avoid, with a gloss.

The RFQ quoting workflow for automotive aftermarket parts: a customer sends an RFQ, the system finds the OE number for each customer part number, looks up MSRP and retail prices, estimates the should-cost, and outputs a pricing worksheet for the pricing team to decide on.

## RFQs and part numbers

**RFQ (询价单)**:
An Excel file sent by a customer, one part to quote per row.
_Avoid_: 订单 (order), 报价单 (quote sheet)

**RFQ line (询价行)**:
One row of an RFQ; the system processes and outputs by row, and each row has a unique row number.
_Avoid_: 记录 (record), 条目 (entry)

**Customer part number (客户件号)**:
The part number the customer wrote in the RFQ, in two forms: as written and after normalization.
_Avoid_: 客户 SKU (customer SKU), 零件号 without a qualifier (part number)

**Normalization (标准化)**:
Unifying how part numbers are written before comparing: full-width to half-width, upper case, and removing spaces and `- _ . /`. Two part numbers that are "equal after normalization" count as the same number.

**Our part number (本公司件号)**:
The number in our own part master data, either given directly in the RFQ or found through the cross-reference.
_Avoid_: 内部件号 (internal part number), 我方 SKU (our SKU)

**Cross-reference (对照表)**:
Known "customer part number → our part number" pairs in the internal database, covering only some customer part numbers.
_Avoid_: 映射表 (mapping table), xref 表 (xref table)

**OE number (OE 号)**:
The number the original equipment maker (vehicle manufacturer) gives a part; the key for looking up MSRP and retail prices.
_Avoid_: OEM 号 (OEM number), 原厂号 (factory number)

**Supersession chain (替代链)**:
The sequence of OE numbers for the same part from old to new, where an old number is replaced by a new one (superseded by / replaces).
_Avoid_: 替换链 (replacement chain), 版本链 (version chain)

**Current OE (当前 OE 号)**:
The last number of the supersession chain, i.e. the OE number currently valid on that chain.
_Avoid_: 最新号 (latest number)

**Primary OE (主号)**:
In a one-to-many case, the one OE number chosen for quoting among candidates from several makers; picked by a person (the ground truth is judged against it too); unrelated to the supersession chain.
_Avoid_: 首选号 (preferred number), 默认号 (default number)

**OE resolution (OE 解析结果)**:
The conclusion of finding the OE number for an RFQ line: uniquely determined (resolved), several candidates that cannot be decided (multiple), or none found (not_found).
_Avoid_: 匹配结果 (match result)

**One-to-many / many-to-one (一对多 / 多对一)**:
One-to-many: one customer part number maps to several OE numbers. Many-to-one: several customer part numbers map to the same OE number, and price results are shared among those rows.

**Shared-OE group (共用 OE 组)**:
Rows whose OE is uniquely determined with the same current OE; within the group, MSRP and retail results all come from the representative row (the group's smallest row number without a row failure on that path). Should-cost is not shared.
_Avoid_: 重复行 (duplicate rows), 合并行 (merged rows)

**Match type (匹配类型)**:
How a candidate OE number was found: the customer part number written exactly the same, equal after normalization, from an interchange lookup, or not found in page text (inferred by the LLM). Used only for OE resolution; do not mix it up with retail match quality.

## Prices

**MSRP (厂商指导价)**:
The suggested retail price published on the OE maker's website; only US dollars are used.
_Avoid_: 原厂价 (factory price), 标价 (sticker price)

**Discontinued (停产)**:
The maker's price page marks the OE number as discontinued; the page may still show a last price.

**Fallback (回退)**:
When the current OE number has no price, walking back along the supersession chain to use an older number's price, noting which older number was used.

**Site (站点)**:
One of the five retail websites; each site gives at most one retail quote per RFQ line.
_Avoid_: 商家 (merchant), 渠道 (channel)

**Retail quote (零售报价)**:
One product listing at a site that matches the part, with list price, sale price, discount rate, effective price and stock status.
_Avoid_: 零售价 (retail price) except when meaning a single number, listing

**List price / sale price / effective price (原价 / 折扣价 / 实际价)**:
The list price is the marked price; the sale price is the promotional price; the effective price is the sale price when there is one, otherwise the list price. Price comparison and outlier detection use the effective price.

**Listed part number (零售标题件号)**:
The part number exactly as written in a retail product title: how the OE number (or an older chain number) is written on a retail site, possibly with different case or spacing. It is neither the OE number itself nor the site's own stock number.
_Avoid_: 零售 SKU (retail SKU), 站点件号 (site part number)

**Match quality (匹配质量)**:
How close the part number in a retail title is to the OE number: written exactly like the current OE (exact), equal after normalization (normalized), listed under an older chain number (fuzzy). Used only for retail quotes.

**Outlier (离群报价)**:
A retail quote whose effective price deviates from the median of all the row's quotes by more than the threshold; it cannot be selected as the reference retail price.
_Avoid_: 异常价 (abnormal price), 错误价 (wrong price)

**Selected retail quote (参考零售价)**:
The quote chosen by rule from the non-outlier quotes as the pricing reference; when every quote is an outlier there is no reference retail price.
_Avoid_: 最终零售价 (final retail price), 推荐价 (recommended price)

## Cost

**Should-cost (应有成本)**:
An estimate, from our part's spec and illustrative rates, of "what it should cost us to make this part": the FOB (free on board) cost, excluding the cost of shipping to the US.
_Avoid_: 成本价 (cost price), 底价 (floor price), 成本 without a qualifier (cost)

**Landed cost (到岸成本)**:
Should-cost (FOB) plus ocean freight, insurance, duty, MPF, brokerage and inland freight, i.e. the cost of the part delivered to a US warehouse; used when comparing with MSRP and retail prices.
_Avoid_: 总成本 (total cost), 落地价 (landed price)

**FOB (FOB)**:
The sum of material, conversion, overhead, SG&A, profit and packaging, i.e. the free-on-board price, which is also the basis for duty.

**Cost breakdown (成本栈)**:
The amount of each item from material to landed cost.
_Avoid_: 成本明细 (cost details), 成本结构 (cost structure)

**Spec (规格)**:
The part attributes needed to compute should-cost: category, material, weight, cycle time.

**Category (品类)**:
The cost category a part belongs to, currently only thermostat and water pump; it determines packaging, freight and the default cycle time.
_Avoid_: 类别 (class), 产品线 (product line)

**Cycle time (cycle time)**:
Minutes needed to produce one part; the category default is used when our part number is unknown.
_Avoid_: 工时 (labor hours), 节拍 (takt)

**Material (材质)**:
The part's main material, named as in the internal database (e.g. aluminum, cast iron, brass, stainless steel); the various wordings on external pages are mapped to these names first.

**Spec source (规格来源)**:
How trustworthy a row's spec is: internal and external agree > internal only > external only > internal and external conflict; missing when either field is absent on both sides. On conflict the internal database wins.
_Avoid_: 数据来源 (data source)

**Insufficient data (数据不足)**:
Computation inputs such as the spec or rates are really missing, so should-cost cannot be estimated; different from a row failure, where the lookup did not succeed.

**Price reference (参考价区间)**:
MSRP, reference retail price and landed cost shown side by side for the pricing team to decide on; the system does not give a final quote.
_Avoid_: 建议报价 (suggested quote), 目标价 (target price)

## Data sources and trust

**Internal DB (内部库)**:
Our own part master data, cross-reference, material prices and cost rates, accessed through MCP; it wins when it conflicts with external pages.
_Avoid_: 数据库 without a qualifier (database), ERP

**External page (外部页面)**:
Noisy text from simulated web lookups: OEM catalog pages, interchange lookup pages, maker price pages, retail product pages, external spec pages.

**Evidence (证据)**:
The source, page or record id, and key source-text snippet supporting a conclusion.

**Grounding (原文核对)**:
A number or phrase extracted by the LLM is used only if it really appears in the page text; this prevents fabrication.
_Avoid_: 校验 (check), 验证 without a qualifier (validation)

**Row failure (行失败)**:
A row could not be looked up at some stage (e.g. search timeout, extraction failure, upstream failure); the failed stage and reason are recorded, other rows go on as usual, and the row can be rerun on its own.
_Avoid_: 报错 (error), 异常 (exception)

**Ground truth (ground truth)**:
The correct answer for each row recorded by the synthetic data generator; used only for evaluation and manual checks, and agents must not read it.
_Avoid_: 标准答案 (answer key), 标注 (labels)

## Evaluation and review

**Confidence score (置信度)**:
A 0–100 score expressing the system's confidence that a row's conclusions as a whole can be used directly.

**Row correct (行正确)**:
A row's OE, MSRP, reference retail site, should-cost status and spec are all correct, with no stage failure; row accuracy is computed from it. Correctly concluding "no OE found" or "several candidates" also counts as row correct.
_Avoid_: 行准确 (row accurate)

**Usable as is (可直接使用)**:
Row correct, and OE uniquely determined, and retail quotes not all outliers: the conclusions can be used for pricing without a person. The confidence score expresses exactly this, and confidence calibration is computed against it.
_Avoid_: 可用 without a qualifier (usable), 通过 (passed)

**Evidence signal (证据信号)**:
0–1 signals computed by deterministic code from each stage's results (OE match type, complete supersession chain, number of price sources, retail price dispersion, whether MSRP and retail prices are plausible, spec source), weighted into the evidence score.

**Gate (封顶)**:
A rule that, in specific situations (e.g. OE not uniquely determined, a stage failure, all outliers), caps the score at the red or yellow ceiling; no strategy can exceed it.
_Avoid_: 惩罚 (penalty), 扣分 (deduction)

**Self-assessment (LLM 自评)**:
In the `evidence_plus_self` strategy, the LLM answers the OE, reference retail price and MSRP independently and states its confidence, sampled twice; whether the answers agree with the system and with each other forms the LLM signal.

**Judge review (judge 复核)**:
In the `evidence_plus_judge` strategy, another model reviews rows whose capped evidence score falls in `judge_band` (default ≥ 50) and gives agree or not plus a score; on disagreement it pushes the score to red, and on agreement it adds nothing. Different from human review.
_Avoid_: 审核 (audit)

**Review reasons (需复核原因)**:
The reasons, listed one by one on the worksheet, why a person should look at this row; filled for every band.

**Band (档位)**:
Confidence split into three bands: green (use directly), yellow (spot check), red (must be reviewed by a person).
_Avoid_: 等级 (grade), 颜色 (color)

**Review (人工复核)**:
A pricing person accepts, modifies or rejects the system's conclusions on red rows one by one. Only on runs created with review enabled, and only after finalize has completed and the worksheet is downloadable.
_Avoid_: 审核 (audit), 审批 (approval)

**Awaiting review (待复核)**:
The state of a run that is finalized with a downloadable worksheet but still has red rows not reviewed; it becomes completed once all are reviewed.
_Avoid_: 暂停 (paused)

## Orchestration

**Run (运行)**:
One complete processing of one RFQ, with a unique run id; it can be interrupted and resumed, and its failed parts rerun.
_Avoid_: 任务 (task), 作业 (job)

**Batch (批)**:
A group of RFQ lines processed and stored together during orchestration (100 rows by default); the next batch starts only after it completes. The four agents run within a batch; resolution, confidence and review run over the whole table at finalize.
_Avoid_: 分片 (shard), chunk

**Failed batch (批失败)**:
A batch that could not finish because of an error row-level fault tolerance could not absorb; nothing from it is stored. Different from a row failure, whose batch still counts as completed.

**Finalize (收尾)**:
Entity resolution, confidence, human review and export over the whole table after all batches.
_Avoid_: 汇总 (summary), 后处理 (post-processing)

**Price leader (领头行)**:
The first row in a run, with a uniquely determined OE, that runs the price agents; other rows with the same current OE reuse its price results instead of looking up prices again.
_Avoid_: 主行 (main row)

**Resume (续跑)**:
Continue an interrupted run from where it stopped: completed batches are skipped, and an interrupted batch reruns only the rows that did not finish.
_Avoid_: 重启 (restart)

**Rescore (重新打分)**:
Create a new run on a finalized run's four-path results with another confidence strategy, redoing only resolution and confidence without rerunning the agents; used to compare confidence strategies on the same results. Error injection copies runs the same way, and every copied run records its source run.
_Avoid_: 重跑 (rerun), 重算 (recompute)

**Injected row (注入行)**:
A row deliberately made wrong in the error injection experiment (OE number, MSRP or reference retail price), used to test whether confidence pushes wrong rows down to yellow or red; being pushed down is called "held back".
_Avoid_: 坏行 (bad row), 脏数据 (dirty data)

**Evaluation (评估)**:
Comparing a run's system conclusions with the ground truth to compute per-stage accuracy, confidence calibration and cost; computed on the system conclusions before review, without calling an LLM.
_Avoid_: 测试 (test), 打分 (scoring)

**Rerun failed (重跑失败)**:
Rerun failed batches and the rows with a row failure in completed batches, then redo finalize.

**Review status (复核状态)**:
The outcome of human review: accepted, modified (OE number or price changed), rejected. Changing the OE number makes the row look up prices and estimate should-cost again; changing a price only overrides the values on the worksheet.

**Run lease (运行租约)**:
The rule that only one process may drive a run at a time: taken when driving starts, kept alive by a periodic heartbeat, released at the end; once the heartbeat expires another process can take over. Both the CLI and the API must take the lease first.
_Avoid_: 锁 without a qualifier (lock)

**Heartbeat (心跳)**:
The timestamp the process holding a lease writes periodically, used to tell whether it is still alive.

**Worker (worker)**:
A separate background process the API starts for a run, which runs a resume or the recomputation after an OE number is changed in review; it is detached from the API process and can be resumed after being killed.
_Avoid_: 后台任务 (background task), job
