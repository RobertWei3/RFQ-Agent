# Phase 7 — 审查后修复清单

来源：review session（2026-10-08），`/code-review c914496`。审查时 fake 模式 `uv run pytest`：401 passed；mypy、ruff 无问题。§17 P7 两项验收（kill / resume、`--review` 可交互）都通过。

## 使用方式
- 按 `/tdd` 修：每条 bug 先写在现有代码上会失败的测试；完成后打勾并注明测试名。
- 需要业务决定的条目先问用户，决定写回 RFQ-SPEC.md 与 phase-7.md。

## A. 必须修（Spec）
- [x] **A1 🔴 rerun-failed 只重跑已完成批中的几行时，整批重新定价**（`batch.py` 的 `_merge` reducer 保留了上一次整批的 OE 结果；`start_batch` 把批设为 running 后，同批其他行的价格又因 `cached_prices` 只认 completed 批而无法复用）。修法：重跑前删除该批 thread 的检查点（`adelete_thread`，thread_id 仍按 §13）；价格缓存改为“结果库中已保存且该路不是行失败的结果”，不再看批状态，但排除本次正在重跑的行（Q3：重跑的行四路全部重跑）。（测试 `test_rerun_failed_reruns_only_the_failed_rows_of_a_completed_batch`；`test_cached_prices_skip_failures_and_the_rows_being_rerun` 替换原缓存测试）
- [x] **A2 🟠 rerun-failed 中途被 kill 后，`--resume` 跳过收尾**（运行状态仍是 completed）。修法：rerun-failed 开始时把运行状态改回 running。（测试 `test_resume_finishes_a_rerun_that_was_killed`）

## B. 低（Spec）
- [x] **B1 复核改价后，该行因其他原因重算置信度时，judge 会看到人工价格**（`judge_brief` 读工作表的参考零售价列）。用户选 A：置信度只按系统结论计算，复核叠加在打分之后（`finalize.resolve` 存未叠加的行，`score_chunk` 打分后再 `apply_reviews`）；写入 §13 与 phase-7.md Q13a。（测试 `test_a_rescored_row_is_judged_on_the_system_price_not_the_manual_one`）
- [x] **B2 复核暂停（awaiting_review）时也导出 JSONL**，与 Q21“收尾后导出”不一致。修法：暂停时不导出，提示用 `--resume` 继续。（`test_review_prompts_red_rows_and_quitting_leaves_the_run_resumable` 补断言）
- [x] **B3 改 OE 号：先保存结果再写 review_log，两步之间被 kill 会重跑三个 agent**。保存在前是对的（反过来会丢失人工 OE）；修法：重放时如果该行已保存同一个人工 OE 的结果，就不再重跑。（测试 `test_a_manual_oe_saved_before_a_crash_is_not_priced_again_on_resume`）

## C. Standards（§0 规则 7 与代码坏味道）
- [x] **C1 跨模块类型不在 schemas.py**：`RunSpec`、`RunRecord`、`BatchRecord`、`ReviewEntry`、`ReviewDecision` 是 CLI / 编排器 / 之后 API 共用的契约，移到 `schemas.py` 并写入 §7。`RowOutcomes`、`StoredRow` 只在编排器和结果库之间使用，属于结果库的持久化模型，留在 `store/runs.py`，在 §7 注明。（`schemas.py` 新增 `RunSpec`、`RunRecord`、`BatchRecord`、`ReviewPrompt`、`ReviewDecision`、`ReviewEntry`，`StrategyName` 移到 schemas 由 config 引用；§7 已补；`test_all_section7_models_present` 更新）
- [x] **C2 Reviewer 传裸 dict**：改为 `Callable[[ReviewPrompt], Awaitable[ReviewDecision | None]]`；interrupt 那一层仍用 JSON，在编排器里转换。（`Reviewer = Callable[[ReviewPrompt], Awaitable[ReviewDecision | None]]`；interrupt 值为 `ReviewPrompt` 的 JSON，resume 值为 `ReviewDecision` 的 JSON）
- [x] **C3 注解里的 Any**：`cli/run.py` 的 `list[Any]` → `list[RFQLine]`；`finalize._stage_inputs` 返回具体类型；`_Session` 的两个图用具体的 `CompiledStateGraph` 类型。（`StageInputs` 类型别名；`BatchGraph` / `FinalGraph` 别名）
- [x] **C4 P7 新写的长函数**：`run_command`、`finish_run`、`RunContext.build` 拆短。P5 / P6 已有的长函数（`_add_self`、`plan_upstream`、`agent_oe_interchange`、`run_downstream_agent`）不是本 Phase 引入的，不在本清单处理。（`run_command` 与 `rerun_failed_command` 共用 `_execute`；`finish_run` 拆出 `_export`、`_compare`；`RunContext.build` 用 `_AgentKwargs`）
- [x] **C5 重复**：“打开 RunStore → try/finally 关闭”抽成 context manager（同时修掉 `rerun_failed_command` 漏写 finally）；`run_command` 与 `rerun_failed_command` 的公共部分合并；状态元组 `("pending", "running")` 抽成常量。（`store.runs.open_store`；`schemas.OPEN_BATCH_STATUSES`）
- [x] **C6 数据泥团**：`deps`、`reviewer`、`trace_path` 合成一个参数对象。四个 agent context 中重复的 `tools / settings / run_id / trace` 不改（agent context 的形状属于 P2–P5，P7 只是构造它们）。（`RunOptions(deps, reviewer, trace_path)`）
- [x] **C7 其他**：删掉只包一层的 `_dump`；`cli/worksheet._strategy` 改为公开函数再引用。（`_dump` 删除，由 `ReviewPrompt` 取代；`cli/worksheet.parse_strategy`）

## 完成标准
- A、B2、B3、C 组完成或注明不做的理由；B1 有用户决定并写回文档。
- fake 模式 `uv run pytest` 全部通过；ruff、`mypy src` 无问题。
- 修完后零成本重跑 20 行样例的 fake 运行，分数与档位与修复前相同（这些修复不改变正常运行的结果）。

## 完成记录（2026-10-08）
- fake 模式 `uv run pytest`：410 passed（新增 A1、A2、B3 的测试和 B2 的断言）；ruff、`mypy src` 无问题。
- 回归：修复后零成本重跑 8000 行 fake（`run-20261008T051125Z-3fabb6`），导出与修复前的运行 A（`run-20261008T043358Z-cad217`）逐字节相同。比原计划的 20 行检查更严。
- B1：用户选 A（2026-10-08），已修复；修复后 `uv run pytest` 411 passed，ruff、`mypy src` 无问题。
