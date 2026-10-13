# Phase 8 — 审查后修复清单

来源：review session（2026-10-08），`/code-review 6f358b6`。审查时 fake 模式 `uv run pytest`：459 passed。§17 P8 两项验收（工作表着色正确、全部接口有测试）都通过。

## 使用方式
- 按 `/tdd` 修，每条 bug 先写在现有代码上会失败的测试；完成后打勾并注明测试名。
- 文本替换每处断言匹配一次。

## A. Spec
- [x] **A1 🟠 心跳任务出异常后悄悄停掉**（`lease.py` 的 `beat()` 不捕获异常；事件循环被同步计算卡住超过 TTL 也会让心跳停掉），租约过期后别的进程可以合法接手，形成两个进程同时驱动。修法：心跳放到独立线程、用独立连接，不受事件循环影响；刷新出错只重试，距上次成功刷新接近 TTL 仍失败时判定租约已丢、停止驱动。（`lease._Heartbeat`；测试 `test_the_lease_stays_fresh_while_the_event_loop_is_blocked`、`test_a_heartbeat_that_keeps_failing_stops_driving_the_run`）
- [x] **A2 🟠 CLI 复核前没有先导出**（Q5：先导出 JSONL 与 xlsx 再逐条询问）。复核途中 Ctrl-C 时运行已是 `awaiting_review`，磁盘上却没有导出。修法：收尾完成、开始询问前导出。（`RunOptions.on_finalized`；测试 `test_the_worksheet_is_exported_before_the_first_review_question`）
- [x] **A3 改 OE 号的 409 缺预计调用数**；这条路径和 `POST /resume` 的真实 LLM 409 都没有测试。（`estimate.review_calls`；测试 `test_work_that_calls_a_real_provider_is_refused_with_its_expected_calls`）
- [x] **A4 worker 在接手前退出，租约挂在 API 的 token 上最多 60 秒**。修法：worker 退出时释放交给它的 token（只在 token 仍持有时生效）。（`cli/run._driving`；测试 `test_a_worker_that_fails_before_taking_over_releases_the_lease_it_was_given`）
- [x] **A5 改 OE 号的 worker 被 kill 后 `/resume` 不可用**（运行是 `awaiting_review`）。恢复方式是重新提交该行的复核；补测试证明可行，写进 §15.1。（测试 `test_a_new_oe_number_killed_midway_is_finished_by_answering_the_row_again`，在现有代码上直接通过）

## B. 需要用户确认
- [x] **B1 I1 / I2 改变了 §13 “心跳过期才能接手”的规则**。用户接受（2026-10-08）。

## C. Standards 与代码坏味道
- [x] **C1** `LLMUsage` 跨 store / output 使用，移到 `schemas.py` 并写进 §7（§0 第 7 条）。租约读数同样改为 `RunLease` 契约。
- [x] **C2 重复**：API 与编排器的复核前检查；`ReviewDecision(row_id=..., **model_dump)` 两处；API 两段 404 判断；CLI 两处租约错误处理；Excel 两处表头样式；store 两处读租约 SQL。（`ensure_reviewable`、`ReviewDecision.for_row`、`ApiContext.row`、`cli/run._driving`、`excel._header`、`RunStore.lease`）
- [x] **C3 成组参数**：`create_run` / `resume` 都依次 `run_calls → guard_real → take_lease → start_worker`；`guard_real` 的 roles 可从 calls 推出。（`ApiContext.run_in_worker`；`guard_real(calls)`）
- [x] **C4 同一分支写两遍**：`estimate.py` 两个函数按 strategy 分支，改为一张表。（`estimate.CONFIDENCE_ROLES`）
- [x] **C5 原始类型**：`RunSummary.bands` / `reviews` 用 `Band` / `ReviewStatus`；`run_calls` 返回值、`lease()` 返回值改为具名类型；覆盖率路径键名只定义一处。（`CallEstimate`、`RunLease`、`CoveragePath`）
- [x] **C6 重复读库**：`is_driven` 读两次租约。
- [x] **C7 命名**：`every` → `heartbeat_s`；`LEASE_HELD_EXIT` 也覆盖租约被接手；`Api` 类名太宽泛。（`heartbeat_s`、`LEASE_EXIT`、`ApiContext`；CLI 的 `_execute` 参数收成 `_Options`）

## 完成标准
- A、C 组完成；fake 模式 `uv run pytest` 全部通过；ruff、`mypy src` 无问题。

## 修复中发现：SQLite 3.51.1 的打开 / 关闭死锁（2026-10-08）
- 重构 C3 后，API 复核测试卡住（12 次中 2 次）。faulthandler：API 线程在关闭结果库连接，worker 线程在同一进程里打开同一文件的连接，两者都停在 SQLite 内部。与 phase-8.md “偶发卡住的诊断”的原生栈相同（`findReusableFd` 与 `sqlite3WalClose → unixLock` 等互斥锁）。
- 原因已查实：这是 SQLite 3.51.0 / 3.51.1 unix VFS 的已知死锁（3.51.0 新增的 “close() 破坏 POSIX 锁” 检测逻辑），3.51.2 修复。本机 Homebrew 的 libsqlite3 是 3.51.1。之前把追踪改成单连接只是减少了触发，API 线程池里并发开关连接仍会触发。
- 修法：进程内所有 SQLite 连接的打开和关闭都经过同一把锁（`store.db.OPEN_CLOSE_LOCK`、`Connection`、`open_sqlite`；结果库、追踪和检查点的 aiosqlite 连接都走它）。修复后同一回路 12 次 0 卡住，整套测试 467 passed。
- 没有能稳定变红的单元测试接口（纯线程开关连接的脚本 5 万次不复现），由上面的回路验证。
- 本机已由用户 `brew upgrade sqlite` 升到 3.53.4（同时带动 Python 3.12.12 → 3.12.15，`.venv` 指向 Homebrew 的 `opt` 路径，照常可用）；升级后 fake 模式 `uv run pytest` 467 passed。代码里的锁仍保留，防止其他环境用到有问题的版本。

## 完成记录（2026-10-08）
- A1–A4 的新测试先在修复前的代码上失败，修复后通过；A5 的测试在现有代码上直接通过（行为本来成立，补测试固定下来）。
- 所有文本替换都断言匹配一次（有一次脚本在中途断言失败，按文件原子写入，重跑剩余部分）。
- fake 模式 `uv run pytest`：467 passed；ruff、`mypy src` 无问题。代码副本上整套测试连续 3 次通过，没有卡住。

## 第二轮审查（2026-10-08，`/code-review e018348`）
结论：上一轮 6 条都已修好，§17 两项验收仍通过，没有阻塞合并的问题，467 passed。I1 / I2 的确认确实是用户在本实现会话中做出的（“接受 I1 和 I2，开始修”）。用户决定修完再合并。

- [x] **R1 心跳阈值没算上 SQLite 的等锁时间**（一次刷新可等锁 30 秒，可能拖过租约过期）。心跳线程的连接改用短的等锁时间（≤ 2 秒且不超过心跳间隔），重试截止为 `ttl − 间隔 − 等锁时间`；配置要求 `lease_ttl_s > 2 × lease_heartbeat_s`。（测试 `test_a_heartbeat_stuck_on_a_lock_gives_the_run_up_before_its_lease_expires`、`test_a_lease_must_outlive_two_heartbeats`）
- [ ] **R2 取消要等主任务下一次 await 才生效**：不修。asyncio 无法从外部打断同步计算；R1 保证在租约过期前就判定丢失，正常运行也没有这么长的同步计算。残余风险记录于此。
- [x] **R3 收尾时可能多收到一次裸取消**：停止心跳时先关掉“判定丢失”的出口，撤销尚未送达的取消，块结束后发现租约已丢就抛 `LeaseLost`。（测试 `test_a_lease_lost_while_the_block_finishes_is_reported_not_left_as_a_cancel`）
- [x] **R4 `stop()` 卡事件循环**：改为 `await asyncio.to_thread(heartbeat.join)`，等锁时间也缩短了。
- [x] **R5 SQLite 版本要求**：§18 写明 ≥ 3.51.2；检测到 3.51.0 / 3.51.1 时发 `RuntimeWarning`（`check_sqlite_version`，测试 `test_a_sqlite_known_to_deadlock_is_warned_about`）；`tracking.py` 注释更正。
- [x] 判断题：`_beat` 改为显式判断（`_refresh` 返回 renewed / taken_over / failed）；`hold_lease` 不在 asyncio 任务里时直接报错；`OPEN_CLOSE_LOCK` 注释写明不覆盖 MCP server、data_gen 和被回收的连接；aiosqlite 构造方式加注释说明；`REASONING_PATHS` 取自 `CoveragePath`；CLI 的 `guard_real` 一次检查所有角色；删除死代码 `RunStore.lease_active`；`_execute` 的参数改名 `opts`。

完成记录：fake 模式 `uv run pytest` 475 passed；ruff、`mypy src` 无问题。
