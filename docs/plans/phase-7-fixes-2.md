# Phase 7 — 第二轮审查修复清单

来源：第二轮 review session（2026-10-08），范围为 11036c4 之后的两个修复提交（b5bd403、ed9c786）。审查时 fake 模式 `uv run pytest`：411 passed。§17 P7 两项验收仍然通过。

## 使用方式
- 同第一轮：按 `/tdd` 修，每条 bug 先写在现有代码上会失败的测试；完成后打勾并注明测试名。
- 本轮没有需要用户做业务决定的事项：都是让代码符合已写进 §13 的规则。

## A. 必须修（Spec）
- [x] **A1 🟠 被 kill 的 rerun 再 resume 时只做完被 kill 的那一批**（还没轮到的重跑批状态仍是 completed，resume 跳过它们，运行却被标成 completed）。根因：重跑计划只在内存里。修法：`rerun-failed` 开始时在一个事务里把重跑计划写进结果库——每个要重跑的批状态改回 `pending`，并记下这次要重跑的行（`batch_results.rerun_rows`，`BatchRecord.rerun_rows`）；之后 resume 和 rerun 走同一个循环，执行一个批时只跑它的 `rerun_rows`（没有就是全批）。批完成时清空 `rerun_rows`。人工 OE 的行每次执行批时都从结果库取，不再只靠 rerun 传入。（`RunStore.reopen_for_rerun`；`_Session.run_open_batches` 由 resume 与 rerun 共用；测试 `test_resume_after_a_killed_rerun_reruns_the_batches_not_reached_yet`、`test_a_rerun_plan_reopens_the_run_and_its_batches`）
- [x] **A2 删除 thread 之后、第一个检查点写入之前被 kill，resume 会按整批重新询价**。由 A1 一并解决：重新开始时用的是结果库里的 `rerun_rows`，而不是整批行号。（批重新开始时的输入取 `rerun_rows`；时间窗太窄，没有单独的测试，由 A1 的机制覆盖）
- [x] **A3 重跑的行可能通过别的批里的副本复用到自己的旧价格**（B 复制了 A 的旧 MSRP，A 重跑时从 B 那里取回）。修法：结果库记录每行价格结果是否为副本（`msrp_from` / `retail_from`：复制来源的行号，自己跑的为空）；缓存只取自己跑出来的结果，并排除未完成批中这次要跑的行。`cached_prices` 自己算排除集合，去掉含义不清的第三个参数。（`PricePlan.copied_from`；测试 `test_a_rerun_row_does_not_get_its_old_price_back_from_a_copy_in_a_later_batch`、`test_cached_prices_are_own_non_failure_results_outside_open_batches`）

## B. Standards
- [x] **B1 `ReviewEntry.ts` 与 §7 不一致**：代码 `ts: str = ""`（写入时由结果库填当前时间），§7 写成必填。改 §7 与代码一致并注明。（§7 改为 `ts: str = ""` 并注明）
- [x] **B2 重复**：`run_batch` 与 `finalize` 的“非恢复则清 thread 再给新输入”抽成一个函数；`store/runs.py` 三处“读全表再在 Python 里按 row_id 过滤”改为 SQL `row_id IN (...)`（复核改 OE 时不再每次扫全表）。（`_continue_or_restart`；`RunStore._select`）
- [x] **B3 命名**：`_open_rows` → `_rows_in_open_batches`（同时按 `rerun_rows` 计数）。
- [x] **B4 静默跳过**：`_restart` 遇到不是 `BaseCheckpointSaver` 的 checkpointer 时报错，而不是什么都不做。（抛 `TypeError`）
- [x] **B5 提示文字**：只有 `awaiting_review` 才打印 “review paused”；其他未完成状态打印实际状态。

## 完成标准
- A、B 组完成；fake 模式 `uv run pytest` 全部通过；ruff、`mypy src` 无问题。
- 补多失败批次的 kill / resume 测试（A1）和跨批副本测试（A3）。
- 8000 行 fake 零成本重跑，导出与运行 A 逐字节相同。

## 完成记录（2026-10-08）
- 两个新测试（A1、A3）先在修复前的代码上跑失败（A1：第二个重跑批的行没被重跑；A3：重跑行从后一批的副本取回旧 MSRP，`oe_msrp` 没有启动），修复后通过。
- 结果库新增列 `batch_results.rerun_rows`、`row_outcomes.msrp_from` / `retail_from`；已有数据库在打开时自动补列（`_add_missing_columns`）。
- fake 模式 `uv run pytest`：414 passed；ruff、`mypy src` 无问题。
- 8000 行 fake 零成本重跑（`run-20261008T055637Z-e26c4d`，在已有的 `data/rfq.db` 上运行，顺带验证补列）：导出与运行 A 逐字节相同。
