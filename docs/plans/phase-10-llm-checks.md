# Phase 10 追加 — 自评加问 MSRP、judge 只拦错并审绿档

分支：`phase-10-llm-checks`（从 main 5443553 建）

## 背景（2026-10-10）
错误注入实验（`phase-10-inject.md`）：40 行注入错行中，evidence 留 22 行在绿档，self 留 18 行，judge 留 28 行。
- 自评只问 OE 号与参考站点，不问 MSRP：绿档里的 MSRP 错行从 6 增到 10。
- judge 只审 50–79 分的行，原本在绿档的错行看不到；同意时按 0.5 / 0.5 混合加分，把没看出来的错行抬上了绿档。

用户决定（按推荐）：1. 自评加问 MSRP；2. judge 也审绿档，同意时不加分；3. 改完后在同一份注入结果上再 rescore 比较。

## 决定（用户已确认，2026-10-10）
| # | 问题 | 决定 |
|---|---|---|
| R1 | 自评 MSRP 问什么 | 系统有 MSRP 报价时（`MSRPResult.quote` 不为空）加第 3 题：LLM 从 MSRP 证据片段中读出它认为的 MSRP（美元数字）并给把握（0–100）。prompt 写明 §11.2 的规则：只用美元、同一号码多个价格取较高价、当前号无价时可用替代链旧号的价格。“与系统一致”= 与系统 MSRP 相对误差 ≤1%（同 §14 的 MSRP 判对口径）；两次采样“一致”= 两次答案相对差 ≤1%。该题信号的算法与 OE、零售两题相同（0.5 × score + 0.5 × consistency），LLM 信号取三题平均 |
| R2 | 自评看到的 MSRP 证据 | 自评摘要新增 “MSRP pages” 一节：系统 MSRP 报价的证据（来源、页面号、原文片段）与所查号码（`matched_part_no`），**不写系统抽出的价格和状态**。没有 MSRP 报价的行不出这一节、不问第 3 题 |
| R3 | judge 审哪些行 | `judge_band` 默认改为 `[50, 100]`：所有没有红档封顶的行（封顶后证据分 ≥ 50）都送 judge。配置项保留，可改回 |
| R4 | judge 同意时 | 不再混合：最终分 = 封顶后证据分（judge 只能降分，不能加分）。explanation 写 “judge agreed (score N): evidence score kept”。concerns 照旧写成 `judge note`（P3） |
| R5 | judge 不同意时 | 不变：混合后再封顶到红档，concerns 写进需复核原因 |
| R6 | 预计调用数 | `expected_llm_calls` 自动按新的 `judge_band` 计算；自评每行仍是 `self_samples` 次（多问一题不多调用） |
| R7 | FakeLLM | 自评 fake 从 MSRP 证据片段里读出价格（取片段中的金额，多个取较高），把握 80；judge fake 不变（同意、70） |
| R8 | 写回 | spec §12.2（自评第 3 题、judge 规则）、§18（`judge_band: [50, 100]`）；`config/settings.yaml`；GLOSSARY “LLM 自评”“judge 复核”的描述 |

## 影响
- 已有运行的分数不变（存的是当时的结果）；之后新跑或 rescore 的 `evidence_plus_self` / `evidence_plus_judge` 运行按新规则。
- P10 的 200 行三策略对比与 20 行 judge 冒烟是按旧规则跑的，结果照旧记录，不重跑。

## TDD 测试接口（seam，沿用 P6 的 seam）
1. `confidence.brief.self_brief`：有 MSRP 报价时含 MSRP 证据与所查号码、不含系统价格；没有时不出该节。
2. `confidence.score_rows`（临时 fake 处理函数）：自评 MSRP 题的一致 / 不一致 / 两次不同的信号；没有 MSRP 报价时只有两题；judge 审绿档（证据分 92 的行也调用）；judge 同意时分数 = 证据分（不加分）；不同意照旧封顶。
3. `expected_llm_calls` 与 `rfq rescore` 的预计调用数按新 `judge_band`。
4. fake 处理函数：自评从片段读 MSRP。

## 真实 LLM（报成本、经同意由用户执行）
在注入运行 `run-20261010T215744Z-fd5f0f` 上再 rescore 两次（新规则）：self 约 288 次、约 $0.17；judge 约 144 次（没有红档封顶的行）、约 $0.33；合计约 $0.5。准确调用数在实现后用不带 `--allow-real` 的 `rfq rescore` 算出再报。然后评估、与旧规则的结果对比，记进本文件。

## 完成记录（2026-10-10）
- fake 模式 `uv run pytest`：634 passed（合并注入实验时 627）；ruff、`mypy src` 无问题。改写的旧测试都是按旧规则写的（judge 只审 50–79、同意时混合；20 行样例 judge 调用 5 次 → 16 次）。
- 真实 rescore（用户执行，同一份注入结果 `run-20261010T215744Z-fd5f0f`，0 错误，预计与实际调用数一致）：self `run-20261011T064920Z-c9ee20` 288 次 $0.180；judge `run-20261011T065020Z-716039` 144 次 $0.330。

| 策略 | 规则 | 绿 / 黄 / 红 | 注入行被拦下 | 留在绿档的错行 | 注入 AUROC | 整体 AUROC | 绿档可直接使用 | 本运行成本 |
|---|---|---|---|---|---|---|---|---|
| evidence | – | 88 / 56 / 56 | 45% | 22 / 40 | 0.565 | 0.819 | 75.0% | $0 |
| self | 旧 | 109 / 35 / 56 | 55% | 18 / 40 | 0.719 | 0.883 | 83.5% | $0.148 |
| self | 新（加问 MSRP） | 116 / 28 / 56 | 42.5% | 23 / 40 | 0.764 | 0.901 | 80.2% | $0.180 |
| judge | 旧 | 128 / 5 / 67 | 30% | 28 / 40 | 0.661 | 0.859 | 78.1% | $0.128 |
| judge | 新（审绿档、同意不加分） | 72 / 44 / 84 | **85%** | **6 / 40** | **0.875** | **0.948** | **91.7%** | $0.330 |

留在绿档的注入行按类型（OE / MSRP / 零售）：新 self 8 / 7 / 8（旧 5 / 10 / 3）；新 judge 2 / 0 / 4（旧 10 / 9 / 9）。

观察：
- 新 judge 明显最好：40 行错行只有 6 行留在绿档；13 行 MSRP 错全部压到红档。它压到红档的 28 行全是注入行（红档 84 = 封顶的 56 行 + 注入的 28 行），没有把正常行误拦到红档；黄档里的 38 行正常行保持黄档（同意不加分），所以绿档从 128 行降到 72 行。代价是调用数从 56 增到 144，200 行 $0.33（每千行约 $1.65）。
- 新 self 没有改善：加问 MSRP 后绿档的 MSRP 错行从 10 降到 7，但 OE 与零售错行增加（5 → 8、3 → 8），整体留在绿档 23 行，比旧规则还多。自评同意时仍按 0.3 权重加分（规则未改），而且 temperature 0.7 的两次采样波动较大；这次的差别不足以说明加问 MSRP 有害或有益。
- 结论：要拦住看起来合理的错误，judge（审全部非红档行、只降分）是有效的；自评作为加分信号用处有限。是否把默认策略改为 `evidence_plus_judge`、自评是否也改为只降分，由用户决定（未改）。

## 用户决定（2026-10-11，按推荐）
1. 默认置信度策略改为 `evidence_plus_judge`（`config/settings.yaml`、`config.py` 默认值、spec §12.2 / §18）。理由：注入实验中只有新 judge 能大量拦下错行（留在绿档 6 / 40），且没有误拦正常行；每千行约多 $1.65。真实 provider 下建运行仍需 `--allow-real`，预计调用数照常打印。
2. 自评不改为只降分：这次结果不稳定（两次采样波动、加问 MSRP 后整体反而略差），再改需要再跑实验验证；有了新 judge 后自评不是主力。保持现状。

## 审查后修复（review session 2026-10-11，`/code-review 5443553`，没有阻塞项；用户同意按表处理）
- [x] **F1** 新模块 `rfq_agent/pricing.py`：`MSRP_TOLERANCE` 与 `msrp_matches(value, reference)`，以参考价为分母（打分时参考价为系统 MSRP，评估时为 truth MSRP）。`confidence/scoring.py` 与 `eval/quick_compare.py` 共用这个函数，置信度不再依赖 eval。两次采样之间对比时，以第一次答案为参考价。
- [x] **F2** `_price_judgment`、`_close` 并入 `_judgment(answers, confidences, system, matches)`：件号和站点用相等判断，MSRP 用 `msrp_matches`。
- [x] **F3** 自评题号改为常量 `OE_QUESTION / RETAIL_QUESTION / MSRP_QUESTION`，与 `MSRP_HEADER` 一起放在 `confidence/brief.py`（`llm_checks` 会导入 fake 处理函数，常量放在 `llm_checks` 会循环导入）。prompt 和 fake 处理函数都引用这些常量；"question 2" 原来也是字符串对接，一并改了。
- [x] **F4** 删除 `wants_msrp`，统一用 `system_msrp(row) is not None` 判断。
- [x] **F5** judge 不同意时最终分 = `min(混合分, 红档上限, 封顶后证据分)`，任何 `judge_band` 配置下都只降分。测试 `test_judge_disagreement_never_raises_a_low_score`：修复前得 49，修复后得 39。
- [x] **F6** GLOSSARY "judge 复核" 与 §12.2 策略表改成 "封顶后证据分落在 `judge_band`（默认 ≥ 50）"。§12.2 写明 MSRP 一致的分母，以及 judge 不高于证据分。
- 不改（记录）：**F7** `judge_band` 的上限 100 在默认配置下不起作用，但这个配置项保留，以后想改回分档时还能用；fake 只识别带 `$` 或 `USD` 的金额，在 fake 模式下可以接受。
- fake 模式 `uv run pytest`：635 passed；ruff、`mypy src` 无问题。
- 已存的真实运行不受影响（分数是当时算好的）。分母变化只影响 1% 边界上的个别价格，`min` 只影响非默认的 `judge_band` 配置，所以不重跑。
