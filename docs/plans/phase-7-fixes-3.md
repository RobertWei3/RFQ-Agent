# Phase 7 — 第三轮审查修复清单

来源：第三轮 review session（2026-10-08），范围为 `81185ab`（相对 ed9c786）。审查时 fake 模式 `uv run pytest`：414 passed。§17 P7 两项验收仍然通过；对抗性检查（计划写入前、批次中途、两批之间、收尾期间、重跑后复核暂停期间被 kill，重复 rerun-failed 等）都通过。

## 使用方式
- 同前两轮：按 `/tdd` 修，每条 bug 先写在现有代码上会失败的测试；完成后打勾并注明测试名。
- 待用户确认：`ReviewEntry.ts` 的 spec 改法（见 C1）。

## A. 必须修
- [x] **A1 🟠 复核暂停时执行 rerun-failed、重跑期间被 kill、再 `--resume`：沿用旧的收尾流程**。`rerun_failed` 只清了各批的检查点，收尾 thread 里还留着上次暂停的复核中断；resume 时 `finalize(resume=True)` 看到有待执行步骤，就接着跑旧的那份，重跑的结果不会重新汇总，运行却被标成 completed。修法：rerun-failed 清批检查点时一并清收尾 thread 的检查点。（`rerun_failed` 先清各批与收尾 thread 再写计划；测试 `test_a_rerun_killed_during_a_paused_review_is_finalized_afresh_on_resume`。实测：审查说的“重跑结果不会重新汇总”不成立——旧收尾流程在复核队列答完后会回到 `resolve` 重新汇总；真正的问题是复核人被问到重跑前的旧行（带失败），这个决定还写进了 `review_log`。测试按这一点断言，修复前失败）
- [x] **A2 resume 前显示的行数与预计调用数没有按 `rerun_rows` 算**（`cli/run.py` 的 `_rows_in_open_batches`）。根因：上一轮那处替换没有生效（替换脚本没有断言匹配），“要跑哪些行 = rerun_rows 或整批”这条规则又写在三处。修法：收拢成 `BatchRecord.rows_to_run()`，三处都调用它。（`BatchRecord.rows_to_run()`，run / store / CLI 三处共用；测试 `test_resume_announces_only_the_rows_a_pending_rerun_will_run`）

## B. 低
- [x] **B1 `fail_batch` 不清 `rerun_rows`**：失败的批下次 rerun-failed 本来就整批重跑，留着过时的计划只会让 `runs show` 显示错。修法：失败时清空。
- [x] **B2 `rerun_rows` 用 None 表示整批，`[]` 也会被当成整批**：`BatchRecord.rerun_rows` 设 `min_length=1`，空列表直接报错，并在字段说明里写明。
- [x] **B3 spec 与代码的顺序不一致**：代码先清检查点、再写计划（两步之间被 kill 不会留下有害状态），§13 写反了。改 spec。（§13 同时写明收尾 thread 也清、复核队列按重跑后的结果重新生成）
- [x] **B4 `cached_prices` 中 MSRP、零售两路判断写法重复**：改为按路循环。`msrp_ok/retail_ok`、`msrp_from/retail_from` 成对出现的“数据泥团”保持现状：它们是结果库的列，拆成结构体反而要多一层转换。（`_own_success`）

## C. 需要确认或记录
- [x] **C1 `ReviewEntry.ts` 的 spec 改动**：第二轮选择改 spec（`ts: str = ""`，为空时由结果库在写入时填当前时间），而不是让调用方必须传时间。用户确认同意（2026-10-08）：时间由结果库在写入时填，调用方不必传。
- [x] **C2 `_add_missing_columns` 的决定**：P7 未合并前，本地 `data/rfq.db` 已经有前几个 P7 提交建的表（验收运行留下的），给新列做的补列迁移让这些库能继续用。spec 没要求，记录在这里：只补本 Phase 新增的列，不做通用迁移框架。

## 完成标准
- A、B 组完成；C1 有用户答复；fake 模式 `uv run pytest` 全部通过；ruff、`mypy src` 无问题。
- 8000 行 fake 零成本重跑，导出与运行 A 逐字节相同。

## 完成记录（2026-10-08）
- 两个新测试先在修复前的代码上失败，修复后通过。
- 本轮所有文本替换都断言匹配一次（上一轮 A2 的漏改就是替换没生效、又没有断言）。
- fake 模式 `uv run pytest`：416 passed；ruff、`mypy src` 无问题。
- 8000 行 fake 零成本重跑（`run-20261008T061731Z-4ce2ab`）：导出与运行 A 逐字节相同。
- C1：用户同意保持 `ts: str = ""`（2026-10-08）。

## 第四轮审查（2026-10-08）
Spec 通过，416 passed，代码层面没有要再改的问题。按建议只改文字：§7 `BatchRecord.rerun_rows` 注释改为“不能是空列表，None 表示全批”并写出 `rows_to_run()`；§13 “复核队列重新生成”补上“只排还没复核过的红档行，已复核的决定保留”。另把 `_own_success` 的 `path` 收紧为 `Literal["msrp", "retail"]`。`store/runs.py` 读库时“空值当作全批”的处理与新约束不冲突，保留。
