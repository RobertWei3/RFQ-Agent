# 实施计划存档

每个 Phase 开工前与 Claude Code 逐轮确认的实施计划和决策记录，随该 Phase 的代码一起提交。
phase-0.md、phase-1.md 是在 P2 开始时根据当时的对话补写的，内容与当时确认的决策一致。

| Phase | 文件 | 提交 |
|---|---|---|
| P0 项目骨架与 LLM 层 | [phase-0.md](phase-0.md) | c7295b0 |
| P1 合成数据、内部库、MCP、mock 工具 | [phase-1.md](phase-1.md) | 54f2253 |
| P2 intake + oe_interchange agent | [phase-2.md](phase-2.md) | 46ab4e1 |
| P3 oe_msrp agent | [phase-3.md](phase-3.md) | 合并提交「Merge phase-3-oe-msrp」 |
| P4 retail_price agent | [phase-4.md](phase-4.md) | 合并提交「Merge phase-4-retail-price」 |
| P5 should_cost agent | [phase-5.md](phase-5.md) | 合并提交「Merge phase-5-should-cost」 |
| P6 实体消解 + 置信度 | [phase-6.md](phase-6.md) | 合并提交「Merge phase-6-resolution-confidence」；review 清单 `phase-6-fixes*.md` |
| P7 Orchestrator（主图、分批、检查点、resume、rerun-failed、人工复核） | [phase-7.md](phase-7.md) | 合并提交「Merge phase-7-orchestrator」；review 清单 `phase-7-fixes*.md` |
| P8 Excel 输出 + FastAPI（运行租约、worker、复核改在收尾后） | [phase-8.md](phase-8.md) | 合并提交「Merge phase-8-excel-api」；review 清单 `phase-8-fixes.md` |
| P9 Streamlit Dashboard（只经 HTTP 调 API） | [phase-9.md](phase-9.md) | 合并提交「Merge phase-9-dashboard」；review 清单 `phase-9-fixes.md` |
| P10 评估脚本 + 策略对比（rescore、eval API 与评估页） | [phase-10.md](phase-10.md) | 合并提交「Merge phase-10-eval」；review 清单 `phase-10-fixes.md` |
| P10 追加：错误注入实验（`rfq eval inject`） | [phase-10-inject.md](phase-10-inject.md) | 合并提交「Merge phase-10-inject」；审查修复记在同一文件 |
| P10 追加：自评加问 MSRP、judge 审绿档且只降分、默认策略改为 `evidence_plus_judge` | [phase-10-llm-checks.md](phase-10-llm-checks.md) | 合并提交「Merge phase-10-llm-checks」；审查修复记在同一文件 |
| P11 README + design-notes 模板 | [phase-11.md](phase-11.md) | 合并提交「Merge phase-11-readme」 |
