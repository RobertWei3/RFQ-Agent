# Phase 7 — Orchestrator：主图、并行、分批、检查点、resume、rerun-failed、人工复核

分支：`phase-7-orchestrator`

## 目标与验收（spec §13、§17）
用 LangGraph 主图把四个 agent、实体消解、置信度串起来，按批运行，带检查点、断点续跑、重跑失败批和红档人工复核。
验收：用 8000 行数据运行，中途手动 kill 进程，`--resume` 后已完成的批不重复执行，最终结果与一次跑完的结果一致；`--review` 模式可交互。

## 已定决策
Q1–Q2 由用户逐条确认；Q3 起由用户授权按推荐方案定（2026-10-07），其中 Q12（复核修改 OE 号后是否重算）是用户点名要问的业务规则，也按推荐定，在计划汇报中单独列出。

### 运行结构
| # | 问题 | 决定 |
|---|---|---|
| Q1 | 多对一的价格复用在批内还是跨批 | 跨批（用户选 B）。每批做 OE、价格、应有成本，四路原始结果落库；价格 agent 先查已完成批中相同 `current_oe` 的结果，有就复用。全部批完成后，在运行级的收尾阶段对全表做消解 → 置信度 →（复核）→ 落库。§13 的节点顺序据此修改 |
| Q2 | 同一 `current_oe` 只跑一次价格 agent 时用谁的输入 | 用户选 A。只对 OE `resolved` 的行按标准化 `current_oe` 去重；用第一次遇到的行（批按行号顺序执行，即组内最小 row_id；它所在的批失败时为之后先跑到的行）的 `OEResolution` 跑 oe_msrp、retail_price；组内其他行复制该结果、只改 `row_id`。批内这次运行是行失败时，批内组员该路记同一失败。后果：编排运行中“组内价格结论不一致”的封顶不会再触发；`price_source_row` 与共用说明照 P6 规则生成 |
| Q3 | 批失败与 `rerun-failed` 的范围 | 批失败 = 批图抛出行级容错兜不住的异常（MCP 起不来、写库出错等），整批不落四路结果，标 `failed`，后面的批照常跑。行失败不算批失败。`rerun-failed` 重跑 `failed` 批的全部行，加上 `completed` 批中任一路为 `RowFailure` 的行（整行四路重跑，按原批分组，新结果替换旧结果），之后重做收尾 |
| Q4 | 价格缓存只认什么 | 跨批复用只取已完成批中**该路不是 `RowFailure`** 的结果（MSRP、零售分开判断）；失败结果不跨批复用，后面的批遇到同一 `current_oe` 会重新跑。缓存直接查结果库 `row_outcomes`（按 `current_oe` 标准化键建索引），不另设缓存表 |
| Q5 | 失败批的行在收尾时怎么办 | 收尾不等全部成功：失败批的每行四路都记 `RowFailure(stage="batch", error=批错误)`，照常进入消解和置信度（触发“任一路行失败”红档封顶），保证工作表行数完整。`rerun-failed` 后这些行被真实结果替换 |
| Q6 | 主图拆分与 thread_id | 两类图，都用 SQLite checkpointer：**批图**（每批一个 thread，`thread_id = f"{run_id}:{batch_id}"`，按 §13）和**收尾图**（`thread_id = f"{run_id}:final"`）。批之间由 Python 外层循环按顺序驱动（一批落库后才启动下一批，§4）。`rerun-failed` 对同一 thread_id 传新输入，重新开始该 thread |
| Q7 | 批图节点 | `load_batch`（从结果库取本批 `RFQLine`）→ Send 每行 → `oe_row`（调用 oe_interchange 子图）→ `plan_prices`（纯函数：已缓存 / 批内领头 / 批内跟随）→ 同一步 Send：领头行的 `msrp_row`、`retail_row`，每行的 `should_cost_row` → `collect`（把领头结果复制给跟随行、缓存结果填给复用行）→ `persist_batch`（一个事务写四路结果并标 `completed`）。state 按 §13：`run_id`、`batch_id`、`lines`、四个按 row_id 的结果字典（字典合并 reducer）、`errors` |
| Q8 | agent 子图的检查点 | 四个 agent 子图改为 `compile(checkpointer=False)`，作为普通函数调用，不把内部状态（页面文本等）写进检查点；检查点粒度是“一行一个 agent”。被 kill 时只有没跑完的行级任务会重跑 |
| Q9 | 收尾图节点 | `resolve`（从结果库读全表四路结果 → `resolve_rows` → 叠加人工修改（Q12、Q13）→ 写 `run_rows`）→ Send 每 `batch_size` 行一块 → `score_chunk`（`score_rows`，写回 `run_rows`；LLM 策略下每块完成即入检查点）→ `review`（仅 `--review`）→ `finish`（标运行 `completed`，写导出文件）。大对象只存结果库，收尾图 state 只放 row_id 列表与计数 |
| Q10 | 结果库表（`data/rfq.db`） | `runs`（run_id、输入、数据集、批大小、策略、是否复核、provider/model、状态、时间）；`rfq_lines`（建运行时 intake 一次写入，resume 不重读 Excel）；`batch_results`（run_id、batch_id、行号范围、状态 pending / running / completed / failed、错误、执行次数 `attempts`、起止时间）；`row_outcomes`（每行四路结果 JSON、`current_oe` 标准化键、是否人工 OE）；`run_rows`（每行 `ResolvedRow` JSON、分数、档位、复核状态，P8 API 读它）；`review_log`。检查点单独文件 `data/checkpoints.db`（新增配置 `storage.checkpoint_path`），测试全部用临时目录（phase-0 Q10） |
| Q11 | resume 的判断 | 以结果库为准：`completed` 批直接跳过（即使检查点里还有残留）；未完成的批看检查点，`aget_state(config).next` 非空（被 kill）就传 `None` 恢复，只重跑未完成的行级任务；没有检查点的批从头跑。`failed` 批 resume 时不重试（交给 `rerun-failed`）。收尾图同理：有未完成检查点就恢复，有挂起的复核就继续复核。`context` 不进检查点，每次调用重新传入 |

### 人工复核（`--review`）
| # | 问题 | 决定 |
|---|---|---|
| Q12 | 修改 OE 号后下游要不要重算（业务规则，按推荐定，单独汇报） | **要重算该行。**用人工输入的号构造 `OEResolution`（`status=resolved`、`current_oe`=输入号、链=[输入号]、本公司件号沿用原结果、候选 `match_type="manual"`、证据 `source="manual_review"`），对这一行重跑 oe_msrp、retail_price、should_cost（会产生 LLM 调用），新结果写回 `row_outcomes` 并标“人工 OE”。复核结束后全表重新消解（共用组可能因此变化），并对结果有变化的行重算置信度。人工修改过的行不会再次进入复核队列。之后的 `rerun-failed` 对这类行不重跑 OE，下游三路仍用人工 OE |
| Q13 | 修改价格 | 只覆盖工作表上的数值：MSRP 和 / 或参考零售价（USD，> 0）。工作表 `msrp_status` 记 `found`，`selected_retail_source` 记 `manual_review`，需复核原因追加“价格为人工输入”；`ResolvedRow` 中各 agent 的原始结果不变；不重跑 agent，不重算置信度。修改记在 `review_log`，每次收尾消解后重新叠加 |
| Q13a | 改过价格的行之后因其他原因重新打分时，置信度用不用人工价格（review B1，用户选 A） | 不用。置信度始终按系统自己的结论计算（包括 judge 看到的行摘要），人工价格在打分之后才叠加到工作表上。理由：置信度衡量系统结论的可信度，P10 校准也按系统结论计算；人工价格已由复核状态 `modified` 标明 |
| Q14 | 接受 / 拒绝 | 接受：只记复核状态。拒绝：行保留在工作表上，数值不变，复核状态记 `rejected`，档位不变。两者都写 `review_log` |
| Q15 | 复核状态放哪 | `WorksheetRow` 新增 `review_status: Literal["accepted","modified","rejected"] \| None = None`（schema_version 升到 1.1），Excel、API、Dashboard 共用；`MatchType` 新增 `"manual"`（`OECandidate` 升到 1.1），配置 `confidence.evidence.oe_match.manual: 1.0` |
| Q16 | 复核顺序与 interrupt 用法 | 逐条串行：`review` 节点每次处理队列中的下一行（红档、未复核，按 row_id），调用 `interrupt(行摘要)`，恢复后记录决定，条件边回到 `review` 直到队列为空。每条决定都单独进检查点，中途退出或 kill 后已答的不丢。不用“并行 interrupt 一次答完”的写法（一次弹出上千行，答一半会全部重来） |
| Q17 | CLI 交互 | 每行展示：客户件号、OE 状态 / 候选 / 替代链、MSRP、各站点报价与离群、参考零售价、应有成本与规格来源、分数、档位、需复核原因、explanation。选项：`a` 接受、`o` 改 OE 号（OE 为 multiple 时可直接输入候选序号）、`p` 改价格、`r` 拒绝、`q` 退出（复核挂起，`rfq run --resume RUN_ID` 继续）。用 `Command(resume=决定)` 恢复 |
| Q18 | 复核记录 | `review_log`：id、run_id、row_id、时间、动作（accept / modify_oe / modify_price / reject）、修改前后的 OE、MSRP、参考零售价、备注、修改前整行快照（JSON） |

### CLI、成本、输出
| # | 问题 | 决定 |
|---|---|---|
| Q19 | 命令 | `rfq run --input X.xlsx [--review] [--strategy] [--batch-size] [--limit] [--dataset] [--allow-real] [--trace/--no-trace]`，打印 run_id；`rfq run --resume RUN_ID`；`rfq rerun-failed RUN_ID`；新增只读的 `rfq runs show RUN_ID`（各批状态与执行次数、档位分布、LLM 调用数与成本），用于 kill / resume 验收。运行参数（数据集、批大小、策略、是否复核）存在 `runs` 表，resume / rerun 沿用 |
| Q20 | 成本拦截 | 运行前按角色打印 provider / model 和预计调用数（OE 每行 3 次；价格按去重后的行数估上限；应有成本每行 1 次；置信度按 `expected_llm_calls`，在收尾前无法精确计算时给上限）；任一角色是真实 provider 且没有 `--allow-real` 就拒绝。resume / rerun-failed 同样要求 `--allow-real`。`.env` 的 `RFQ_LLM_PROVIDER="fake"` 会强制 fake，真实运行要在命令行覆盖（phase-4 I5） |
| Q21 | 文件输出 | 收尾后导出 `data/runs/run_<run_id>.jsonl`（每行一条 `ResolvedRow`，与 `rfq worksheet build` 同格式）+ `.meta.json` + `.trace.jsonl`（resume 时追加）；有 ground truth 时生成 `.compare.md`（复用 P6 的工作表对照）。Excel 留给 P8 |
| Q22 | 并发 | 批内全部行并行（Send），LLM 并发受全局信号量（默认 8）限制；一个进程只开一个 MCP 会话，所有批共用 |
| Q23 | Pydantic 进检查点 | 用 `JsonPlusSerializer(allowed_msgpack_modules=[…])` 登记 state 中出现的 schema 类型，消除 “unregistered type” 警告并避免将来版本拒绝反序列化 |

### 遗留整理
| # | 问题 | 决定 |
|---|---|---|
| Q24 | phase-5 的 `run_price_agent` / `PriceAgentRun` | 改名 `run_downstream_agent` / `DownstreamAgent`（三个下游 agent 共用，不只是价格）；`with_lines` 布尔分支改为每个 agent 自带的“输入构造”函数（should_cost 配对 RFQ 行，另两个直接用 OE 结果）。行为与 CLI 选项不变 |
| Q25 | phase-6-fixes-3 可选项 | `expected_llm_calls` 与 `_score_row` 共用一个小函数算“信号 → 证据分 → 封顶 → 是否调用自评 / judge”。phase-6 末尾三个待决问题不在 P7 处理 |

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| I1 | agent 子图在编排器节点里被调用时继承父图的 `durability="sync"`，而子图没有 checkpointer，会去等一个不存在的检查点 future 而报错（langgraph 1.2.12，`pregel/main.py:3461`） | `common.run_row` 显式传 `durability="exit"`（显式参数优先于继承值），并屏蔽随之出现的 “durability has no effect” 警告（模块级 + pytest 配置）。agent 单独运行时行为不变 |
| I2 | 收尾时哪些行要重新算置信度 | `run_rows` 存每行“消解后、打分和复核叠加前”的指纹（basis）和置信度追加的需复核原因；重新收尾时指纹没变的行沿用原分数，不再调用 LLM。复核修改价格不改变指纹，所以不重算（Q13）；改 OE 号会改变指纹，所以重算（Q12） |
| I3 | 复核决定写进工作表的时机 | 复核节点只写 `review_log`（改 OE 号时先重跑下游三路）；队列答完后回到 `resolve` 统一叠加，保证“复核后”与“之后再次收尾”的行完全一致；新出现的红档行会继续进入队列，已复核的行不会再问 |
| I4 | 复核节点被 kill 后重放 | 节点从头执行；某行已有 `review_log` 记录就跳过记录和重跑，避免重复写日志或重复调用 agent |
| I5 | 人工输入的 OE 号写法 | NFKC、去首尾空白、转大写后作为 `current_oe`（不去横杠，保留显示写法） |
| I6 | Send 节点的输入类型 | 每类 Send 有自己的 TypedDict（`OERowInput` 等），节点第一个参数必须名为 `state`（LangGraph 节点协议按名字匹配，mypy 检查） |
| I7 | 批序号与行号 | 批号从 1 开始；批的行号按 row_id 排序切分，`batch_results.row_ids` 记具体行号（intake 跳过的行不占位置） |

## 验收方案
1. 生成 `rfq data generate --n-parts 8000 --seed 42`。
2. fake 模式完整跑一次（基准），记 run A。
3. fake 模式再跑 run B，在若干批完成后手动 `kill -9`；`rfq runs show B` 确认已完成批数；`rfq run --resume B` 跑完。
4. 检查：被 kill 前已完成的批 `attempts` 都是 1；B 与 A 的导出 JSONL 去掉 run_id 后逐行相同。
5. 自动化测试另做一个小规模版本（几百行、小批大小，子进程 SIGKILL 后 resume，与一次跑完的结果比较），放进 `uv run pytest`。

## 8000 行 kill / resume 验收（fake 模式，2026-10-08）
数据：`rfq data generate --n-parts 8000 --seed 42`；批大小 100，共 80 批；evidence 策略。命令行直接用 `.venv/bin/rfq`（经 `uv run` 时 `kill -9` 只杀掉 uv 外壳，Python 子进程会继续跑）。

| 运行 | 做法 | 结果 |
|---|---|---|
| A `run-20261008T043358Z-cad217` | 一次跑完 | 80 批完成，219 秒；35,039 次 fake 调用；绿 4705 / 黄 1855 / 红 1440；按 P6 的“行正确”对照 8000/8000 |
| B `run-20261008T043748Z-cdcf28` | 第 30 批完成后立即 `kill -9`，再 `rfq run --resume` | kill 时 30 批 completed、1 批 running、49 批 pending；MCP 子进程随之退出。resume 148 秒。第 1–30 批 attempts 全为 1，只有第 31 批为 2。导出 JSONL 与 A 逐字节相同（sha1 `18237673…`），`row_outcomes`、`run_rows` 也相同；各 agent 的 LLM 调用数与 A 相同 |
| C `run-20261008T044224Z-21d4c1` | 第 30 批完成后再等 1.5 秒 `kill -9`（落在第 31 批的价格步骤），再 resume | 只有第 31 批 attempts 为 2；该批 100 行的 OE 任务只跑了 1 次（检查点已保存），kill 前已完成的 19 个 MSRP、6 个零售任务没有重跑，其余重跑；导出与 A 逐字节相同 |

B 中第 31 批被 kill 时 OE 步骤还没有任务完成，所以 100 行 OE 都重跑了一次；C 证明了已完成的行级任务确实不重跑。kill 期间用 sqlite3 命令行轮询结果库出现的 “database is locked” 来自轮询本身（读被写锁挡住），运行本身没有报错。

## 真实 LLM 冒烟测试（deepseek-flash，2026-10-08，经用户同意，由用户在终端运行）
命令：`RFQ_LLM_PROVIDER=deepseek .venv/bin/rfq run --input data/generated/n200_s42/rfq_sample_20.xlsx --dataset data/generated/n200_s42 --batch-size 7 --allow-real`（在会话里由我执行时被自动权限检查拦下，改由用户执行）。
运行 `run-20261008T045500Z-0df747`：3 批全部 completed、attempts 1；85 次调用、0 错误，约 7.1 万输入 / 1.9 万输出 token，$0.044（运行前估计约 85 次、约 $0.05）。
分 agent：oe_interchange 42 次 $0.018，oe_msrp 14 次 $0.006，retail_price 15 次 $0.015，should_cost 14 次 $0.006。价格 agent 各只启动 19 行：第 19 行（第 3 批）与第 12 行（第 2 批）共用 OE `1328.97.109`，跨批复用了第 12 行的价格结果（`price_source_row` = 12）。
档位 绿 11 / 黄 5 / 红 4，按 P6 的“行正确”对照 20/20，与 P6 零成本检查（用 P2–P5 单独运行的输出）一致。
同 P2–P6：合成页面格式规整，这个结果只说明整条流水线在真实模型下能跑通，不代表真实数据上的准确率。

## TDD 测试接口（seam，已确认）
1. `orchestrator.prices.plan_price_jobs(oe_outcomes, cached) -> PricePlan` 与 `expand_price_results(...)`（纯函数）：领头 / 跟随 / 缓存复用、非 resolved 不去重、失败结果不跨批复用、批内失败组员同失败。
2. `store.runs`（临时 SQLite）：建运行、批状态与 attempts、四路结果读写、按 `current_oe` 查缓存、`run_rows`、`review_log`。
3. `orchestrator.review.apply_decision(row, decision)` 等纯函数：接受、拒绝、改价格叠加、人工 OE 的 `OEResolution` 构造。
4. `Orchestrator`（async，注入 fake LLM、内存版内部库、临时库与检查点）：`start(...)`、`resume(run_id)`、`rerun_failed(run_id)`、`reviewer` 回调（脚本化复核决定）。覆盖：多批跑完与一次性结果一致、跨批价格只跑一次（`llm_calls` 计数）、注入故障让某批失败后 rerun-failed、模拟中途崩溃后 resume 不重跑已完成批、复核四种动作与改 OE 后的重算、复核中途退出再恢复。
5. CLI（CliRunner + fake）：`rfq run`、`--resume`、`rerun-failed`、`--review` 的交互输入、`rfq runs show`、真实 provider 无 `--allow-real` 时拒绝。
6. 子进程 kill / resume 测试（第 5 条验收）。
另：Q24、Q25 的整理由现有测试保证行为不变。

## 实施顺序
1. 写回 RFQ-SPEC.md（§7、§13、§18）和 GLOSSARY.md；加依赖 `langgraph-checkpoint-sqlite`。
2. 遗留整理（Q24、Q25）单独提交，现有测试全部通过。
3. `/tdd`：seam 1 → 2 → 批图 → 收尾图 → 3 → 复核循环 → 4 → CLI 5 → 6。
4. 8000 行 fake 的 kill / resume 验收（上面的验收方案）。
5. 报真实 LLM 冒烟（20 行样例）的预计调用数和成本，经同意再跑。

## 查证的文档事实（2026-10-07）
- 已装 `langgraph` 1.2.12、`langgraph-checkpoint` 4.2.0。SQLite checkpointer 是单独的包 `langgraph-checkpoint-sqlite`（3.1.1，要求 `langgraph-checkpoint>=4.1,<5`，带 `aiosqlite`）。agent 都是 async，用 `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`（`async with AsyncSqliteSaver.from_conn_string(path)`）；同步的 `SqliteSaver` 不支持 `ainvoke`。
- `Send` 从 `langgraph.types` 导入，条件边返回 `[Send(node, payload), ...]`，结果经 reducer 合并，顺序按 Send 顺序；Send 目标可以是编译后的子图；`context` 会传到 Send 目标和子图。
- 断点恢复：每个任务完成即写入检查点（pending writes）；被 kill 后用同一 `thread_id` 以 `None` 输入恢复，只重跑未完成的任务（已用硬 kill 实测）。状态判断：`aget_state(config).next == ()` 为完成；`next` 非空且有 `interrupts` 为等待复核；`next` 非空且无 interrupt 为被中断。默认 durability 为 `"async"`，`"exit"` 不能从中途崩溃恢复。
- 用非 `None` 输入调用一个有未完成任务的 thread，会丢弃这些任务、重新开始；恢复只能传 `None` 或 `Command`。
- `context` 不存进检查点，每次恢复都要重新传入。
- `interrupt()` / `Command(resume=...)` 从 `langgraph.types` 导入，需要 checkpointer。同一步中多个并行 interrupt 一起出现在 `result["__interrupt__"]`，每个有唯一 `id`；必须用 `Command(resume={id: value, ...})` 一次答完，只答一部分会让所有被中断的任务从头重跑。恢复时节点从头执行，`interrupt()` 之前的代码会重复；resume 值不能为 `None`。
- state 中的 Pydantic 对象可以序列化，但 checkpoint 4.2.0 对未登记类型打印 “Deserializing unregistered type …” 警告；可用 `JsonPlusSerializer(allowed_msgpack_modules=[...])` 或 `LANGGRAPH_STRICT_MSGPACK=true` 登记。reducer 列表在每个变化的检查点中整份保存。
- 子图 `compile(checkpointer=None)`（默认）继承父图的 checkpointer，在节点函数里 `ainvoke` 调用的子图同样继承；`checkpointer=False` 则作为普通函数调用。
