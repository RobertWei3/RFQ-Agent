# Phase 10 — 审查后修复清单

来源：review session（2026-10-09），`/code-review 881d4b0`（881d4b0...84c05eb，11 个提交）。§17 P10 四项验收全部通过；fake 模式 601 passed。

## 使用方式
- 按 `/tdd` 修，行为改动先写在现有代码上会失败的测试；完成后打勾并注明测试名。纯重构靠已有测试保持全绿。
- 文本替换每处断言匹配一次。
- 本清单不调用真实 LLM；改动不影响分数与档位（judge / self 的 prompt 不变），已有的真实运行结果仍然有效。

## A. Spec
- [x] **A1 覆盖率-准确率曲线没有标出选出的阈值**（§15.2 “标出目标准确率的阈值”）。现在只有 0.95 的横线，阈值只在表里。修法：报告 PNG 与 Dashboard 曲线都在目标准确率 95% 的最低阈值处画一条竖线并标注 “t=50 (95%)”；没有达到目标时不画。（`charts.coverage_chart` 断言 spec 中有该竖线；报告侧断言 PNG 仍生成）（`dashboard.charts.threshold_marks`；测试 `test_the_threshold_reaching_the_target_accuracy_is_marked_on_the_curve`、`test_no_mark_when_the_target_accuracy_is_never_reached`，修复前失败；报告 PNG 用 `axvline`，已目视检查）
- [x] **A2 truth 自动查找范围窄**：API 上传的数据集自有询价单（如 `<uuid>_rfq_200.xlsx`，文件在 `data/uploads/`）找不到 truth；`find_truth` 没有对路径 `resolve_path`。修法：去掉上传前缀后，若数据集目录里有同名询价单（`<dataset>/<原文件名>` 存在）即视为数据集自有询价单（不限上传文件，任何目录里的同名文件都按此处理；用错时由客户件号逐行校验拦下），用 `<dataset>/ground_truth.jsonl`；`dataset_dir` 按项目根目录 `resolve_path`（只看文件名，所以 `input` 不需要解析）。spec §14 同步改写。（测试：上传名的 `rfq_200.xlsx` 能找到 `ground_truth.jsonl`，修复前失败）（测试 `tests/test_eval_truth.py`：`test_an_uploaded_dataset_workbook_uses_the_dataset_truth`、`test_relative_and_absolute_paths_are_compared_resolved` 修复前失败；spec §14 已改）
- [x] **A3 spec 说 rescore “全部批记为 completed”，代码原样复制批状态**（失败批仍为 failed）。代码更合理（§14“批失败的行按行失败算错”），改 spec §13 那句话，不改代码。（spec §13 已改）
- [x] **A4 `SiteEval.expected` 的定义少了一个条件**：代码只统计 truth 中 OE 为 resolved 的行（其余行按规则不查零售价）。改 spec §7 注释与 §14 口径，不改代码。（spec §7、§14 已改）

## C. 代码坏味道
- [x] **C1 对比表的列与 95% 目标写了两份**（`report.render_compare_markdown` / `dashboard/charts.compare_frame`）。Dashboard 不能 import 后端，所以不合并；加一个测试（测试侧可以同时 import 两边）断言两边列数相同、目标值相同，列一改不同步测试就红。（`test_the_dashboard_comparison_matches_the_report_comparison`）
- [x] **C2 rescore 的“源运行未收尾”检查做了两次**（`cli/run.py` 与 `store.copy_for_rescore`）。修法：结果库提供一个 `check_rescorable(run_id)`（未收尾、有人工复核都在这里判断），`copy_for_rescore` 调它；CLI 在打印预计调用数之前也调它（这样被拒时不会先打印一段调用数），规则只有一份。（`RunStore.check_rescorable`）
- [x] **C3 百分比 / 比率小函数重复**：`report._rate` 与 `metrics._share` 相同，`cli/eval.pct` 与 `report._pct` 相同。修法：`report` 用 `metrics` 的比率函数；`cli` 用 `report` 公开的 `pct`。（`metrics.share`、`report.pct`）
- [x] **C4 `agent_usage` 与 `llm_usage` 是同一套 SQL 合计**。修法：`llm_usage` 由 `agent_usage` 的结果求和得到（未定价时成本为空的规则不变），已有 `RunSummary` 测试保持通过。（`RunSummary` 与 `agent_usage` 的测试照常通过）
- [x] **C5 `render_eval_markdown` 约 135 行**（§0 第 7 条“函数短小”）。按小节拆成每节一个函数（OE、MSRP、零售、应有成本、校准、覆盖率、成本），主函数只负责拼接。（拆成 `_intro`、`_oe_section` … `_usage_section`；拆分前后对同一份评估渲染的 markdown 逐字相同）
- [x] **C6 `from rfq_agent.cli import eval` 遮蔽内置 `eval`**。模块改名 `cli/evaluate.py`，命令名仍是 `rfq eval`。
- [x] **C7 rescore 的调用数估算放在 CLI**（`cli/run.py` `announce_rescore`）。估算部分移到 `orchestrator/estimate.py`（`rescore_calls(settings, strategy, rows) -> Calls`），CLI 只打印与拦截，与 `run_calls` 的用法一致。（`estimate.rescore_calls`）
- 不改（判断题，记录理由）：
  - **C8 `RunEval.created_at: str`**：与 `RunRecord.created_at`、`ReviewEntry.ts` 等现有契约一致（ISO 8601 字符串，API 与 Dashboard 按 JSON 读），单独改成 `datetime` 反而不一致。

## 待用户决定（不在本清单内改）
见 `phase-10.md` 末尾：1. 档位阈值 / 权重是否按这次结果调整（建议不调）；2. 是否换更难的数据再做一次策略对比（约 $0.7）。

## 第二轮审查（2026-10-09，`/code-review 84c05eb`）
无阻塞问题。用户选择修以下几条后合并：
- [x] Dashboard “按目标找阈值”写了两次：合并为 `charts._pick`。
- [x] 写死的 “95%” 标签：Dashboard 列名与一致性测试的预期列名都由目标值生成（报告表头本来就是）。
- [x] `check_rescorable` 改为只检查、不返回值。
- [x] `report.pct` 补 docstring。
- 不改：`rescore_calls` 与 `_confidence_calls` 开头两三行相同，合并反而更绕。
- A2 措辞：计划原文写“上传”，实际按文件名匹配任何目录的同名文件；已改上面 A2 的说明（误匹配由客户件号校验拦下）。
