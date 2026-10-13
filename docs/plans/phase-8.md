# Phase 8 — Excel 输出 + FastAPI

分支：`phase-8-<slug>`（实施会话从 main 建）

## 目标与验收（spec §12.3、§15.1、§17）
定价工作表 Excel（按档位整行着色、冻结表头、第二个 sheet 为运行汇总）；FastAPI 的 §15.1 全部接口。
验收：工作表着色正确；全部接口有测试。

## 开工前已定的决策（P7 合并后讨论，2026-10-08）
用户同意以下全部按推荐方案定。P8 的 /grilling 从这里接着问其余问题，不再重复这几条。

| # | 问题 | 决定 |
|---|---|---|
| P1 | API 提交复核时怎么接上收尾图挂起的 `interrupt` | 改造复核节点，让 interrupt 一次交出**整个待复核队列**；resume 值是“对第 X 行的决定”（`ReviewDecision` 增加 `row_id`）。节点校验 X 仍在队列中，记录决定（改 OE 号时重跑下游三路），再用剩余队列重新挂起。API `POST /runs/{id}/rows/{row}/review` = 校验 + `Command(resume=...)`；接受 / 拒绝 / 改价同步完成，改 OE 号返回 202、后台完成。CLI 仍逐条取队列第一行，用法不变。复核只有 interrupt + 检查点一条路径，规则仍由 `record_decision` / `apply_reviews` 共用。不采用“API 只接受当前挂起行”（A）和“API 绕过 interrupt 直接写 review_log”（B） |
| P2 | 复核与收尾的先后（业务规则） | **先收尾完成、工作表可下载，复核在已完成的运行上进行**；每条复核决定触发一次重新汇总（指纹没变的行沿用原分数）。CLI `--review` 的交互方式保持不变 |
| P3 | 后台运行怎么跑 | `POST /runs` 写入运行后启动独立 worker 子进程执行 `rfq run --resume RUN_ID`，立即返回 run_id。不用 FastAPI `BackgroundTasks`（在 API 进程内，重启即中断），不引入 Celery / RQ |
| P4 | CLI 与 API worker 同时驱动同一个运行 | 运行租约：`runs` 表加 `owner`（进程号 + 主机）和 `heartbeat`；开始驱动时用一条 `UPDATE ... WHERE owner 为空 OR 心跳过期` 原子抢占，抢不到明确报错；持有期间约每 10 秒刷新心跳，约 60 秒未刷新视为过期可接手。API 提交复核做 resume 时同样先取租约 |
| P5 | SQLite 多进程读写 | 结果库与检查点库都开 `journal_mode=WAL` 并设 `busy_timeout`（P7 8000 行验收时外部轮询出现过 “database is locked”） |
| P6 | 进度从哪来 | `GET /runs/{id}` 的进度（已完成批数 / 总批数）直接读 `batch_results` |

## /grilling 决定（2026-10-08，用户授权全部按推荐方案定）

### 复核
| # | 问题 | 决定 |
|---|---|---|
| Q1 | 哪些运行能复核、运行状态 | 只有 `RunSpec.review=True` 的运行能复核（`POST /runs` 有 `review` 表单字段）。收尾完成即可下载工作表与 JSONL；仍有未复核红档行时状态为 `awaiting_review`，队列空为 `completed`；复核中改 OE 号新变红的行继续入队。`review=False` 的运行收尾后直接 `completed`，提交复核 409。状态取值不变，`awaiting_review` 的含义改为“已收尾、工作表可下载、还有红档行待复核” |
| Q2 | interrupt 的值 | 新模型 `ReviewQueue{run_id, row_ids}`（只放行号，不放整行，避免检查点过大）。`ReviewPrompt{run_id, remaining, row}` 保留为 CLI 复核回调的输入，由编排器取队首行组装。`ReviewDecision` 增加 `row_id`；API 请求体为 `ReviewInput`（`ReviewDecision` 去掉 `row_id`），行号取自路径 |
| Q3 | 复核节点 | 收到决定后校验 `row_id` 仍在队列中（不在就忽略、重新挂起），记录决定，回到 `resolve` 重新汇总，再用剩余队列挂起（P2：每条决定触发一次重新汇总）。`record_decision` / `apply_reviews` 不变 |
| Q4 | API 提交复核 | 依次校验：运行与行存在（404）；运行开了复核且状态为 `awaiting_review`（409）；该行在当前挂起的队列中（409：已复核或不是红档）；取租约（被占用 409）。接受 / 拒绝 / 改价在 API 进程内 `Command(resume)`，返回 200 与更新后的行；改 OE 号由 API 取得租约后交给 worker 子进程执行 resume，返回 202 |
| Q5 | CLI | `--review` 用法不变；按 P2 先导出 JSONL 与 xlsx 再逐条询问（取代 P7 B2 的“暂停时不导出”） |

### 租约与 worker
| # | 问题 | 决定 |
|---|---|---|
| Q6 | 租约细节 | `runs` 新增 `lease_owner`（不透明 token `pid@host:uuid`）、`lease_heartbeat`。一条 UPDATE 原子抢占（owner 为空、是自己、或心跳超过 `storage.lease_ttl_s`=60 秒）；asyncio 任务每 `storage.lease_heartbeat_s`=10 秒刷新，刷新时发现 owner 已被换掉就停止驱动；结束时 finally 释放。抢不到报错并写明占用者与距上次心跳秒数。取租约的操作：`rfq run`（新建与 `--resume`）、`rerun-failed`、worker、API 复核；只读操作不取 |
| Q7 | API 与 worker 的交接 | API 先用自己生成的 token 取得租约，再通过隐藏参数 `--lease-token` 交给 worker，避免返回到 worker 启动之间被 CLI 抢走 |
| Q8 | worker 进程 | `Popen([sys.executable, "-m", "rfq_agent.cli", "run", "--resume", ID, ...], start_new_session=True)`，与 API 进程脱离；日志 `data/runs/run_<id>.worker.log`；后台线程回收进程（不留僵尸进程）。启动器经 FastAPI 依赖注入，测试可替换 |
| Q9 | 被 kill 的 worker 怎么续跑 | 新增 `POST /runs/{id}/resume`（§15.1 补充）：租约空闲或过期且运行未完成 → 启动 worker，202；否则 409。API 启动时不自动续跑。`RunDetail.lease_active` 让前端显示“已停止，可续跑” |
| Q10 | WAL | `store.db.connect` 设 `journal_mode=WAL`、`busy_timeout=30000`；检查点库的 aiosqlite 连接设 `busy_timeout`（`AsyncSqliteSaver.setup` 自己会开 WAL，已查源码） |
| Q11 | 真实 LLM 拦截 | `rfq api serve [--allow-real]`；未加且配置为真实 provider 时，`POST /runs`、改 OE 号、`POST /resume` 返回 409，并给出预计调用数；加了则 worker 也带 `--allow-real` |

### Excel（§12.3）
| # | 问题 | 决定 |
|---|---|---|
| Q12 | 生成时机 | `output/excel.py` 的 `build_workbook(rows, summary)`，数据取自 `run_rows`。`GET worksheet.xlsx` 每次请求现场生成（总能反映最新复核）；运行未收尾 409。CLI 收尾后在 JSONL 旁写 `run_<id>.xlsx` |
| Q13 | 列 | 英文 snake_case 表头（与 API 字段同名）：row_id、customer_part_no_raw、customer_part_no、our_part_no、oe_status、current_oe、supersession_chain（` -> ` 连接）、oe_candidates、参考价区间三列 msrp / selected_retail_price / landed_cost（§12.1 并列）、msrp_status、各站点一列、selected_retail_source、retail_outliers、should_cost_fob、shared_oe_rows、price_source_row、shared_prices_disagree、confidence_score、band、review_status、review_reasons / failures（换行分隔）、四路 reasoning 各一列 |
| Q14 | 着色与冻结 | 按档位整行填色：绿 `C6EFCE`、黄 `FFEB9C`、红 `FFC7CE`（Excel 内置好 / 中 / 差）；无档位不填。复核状态不改颜色。`freeze_panes="B2"`（表头与 row_id 列），表头自动筛选 |
| Q15 | 汇总 sheet | 与 `GET /runs/{id}` 共用 `RunSummary`：行数、各档数量、复核数（接受 / 修改 / 拒绝 / 待复核）、四路覆盖率（OE resolved；MSRP found 或 discontinued；有参考零售价；应有成本 estimated——按系统结果算，不含人工改价）、有行失败的行数、LLM 调用 / token / 成本、耗时。耗时 = `finished_at − created_at`（新列 `runs.finished_at`，收尾完成时写入），为墙钟时间，含 kill 后的停机时间，表中注明 |

### API（§15.1）
| # | 问题 | 决定 |
|---|---|---|
| Q16 | 新增模型（§7） | `RunProgress`、`RunSummary`、`RunDetail{run, progress, summary \| None, lease_active}`、`RowPage{total, offset, limit, items: list[WorksheetRow]}`、`RowDetail{row, reviews, in_review_queue}`、`ReviewInput`、`ReviewOutcome{run_id, row_id, done, row}`、`ReviewQueue`；`RunRecord` 加 `finished_at` |
| Q17 | `POST /runs` | multipart：`.xlsx` 文件 + 可选 strategy、batch_size、review、limit、dataset。文件存 `storage.upload_dir`（默认 `data/uploads`）下 `<uuid>_<文件名>`；服务端 intake，失败 422；建运行、取租约、启动 worker，返回 202 与 `RunRecord` |
| Q18 | 列表与过滤 | `GET /runs` 全部 `RunRecord`，新的在前。`GET /runs/{id}/rows`：`band`、`status` 可重复；`status` 指复核状态 `unreviewed / accepted / modified / rejected`；`offset` 默认 0，`limit` 默认 100、最大 1000；按 row_id 排序，SQL 过滤 |
| Q19 | `GET /runs/{id}/eval` | 评估格式由 P10 定义；P8 注册路由，统一 404“该运行还没有评估结果”，测试覆盖 |
| Q20 | 启动与依赖 | `create_app(settings, launcher)`；`rfq api serve --host 127.0.0.1 --port 8000 [--allow-real]`（uvicorn）。依赖 fastapi、uvicorn、python-multipart；dev 依赖 httpx（TestClient） |

## TDD 测试接口（seam，已确认）
1. `output.excel.build_workbook`：openpyxl 读回检查列、整行填色、冻结窗格、汇总 sheet。
2. `store.runs` 租约：抢占、过期接手、心跳、释放、owner 被换后心跳失败；WAL pragma。
3. 收尾图的队列式复核：编排器测试的脚本化复核人 + 直接 `Command(resume)` 答任意队列行（API 用法）；原有复核测试照常通过。
4. `RunSummary` / `RunProgress` / 行过滤分页（store 层）。
5. API：`TestClient` + 注入启动器（进程内 fake 运行），全部接口的成功与错误码。
6. 子进程验收：API 启动真实 worker 子进程（fake）跑完；中途 kill -9 后 `POST /resume` 跑完且结果与一次跑完相同；worker 持租约时 `rfq run --resume` 被拒。

## 实施顺序
1. 写回文档（下节），单独提交。
2. seam 2 → 3 → 4 → 1 → 5 → 6；脚本替换每处断言匹配一次。
3. `test_stdio_server_via_cli` 若复现卡住，用 /diagnosing-bugs。
4. 真实 LLM 冒烟：P8 不新增 LLM 调用，默认不跑；如需验证 API 驱动的真实运行，20 行约 85 次调用、约 $0.05，经用户同意由用户用 `!` 执行。

## 实施中追加的决定
I1、I2 改变了 §13 “心跳过期才能接手”的规则，审查后用户确认接受（2026-10-08）。

| # | 问题 | 决定 |
|---|---|---|
| I1 | 被 `kill -9` 的进程留下的租约要等 60 秒才能接手 | 租约 token 是 `pid@host:随机`；同一主机上 token 的进程已不存在时立即接手（比较并替换，`take_over_lease`），不等心跳过期。`GET /runs/{id}` 的 `lease_active` 同样把这种租约算作无人驱动。不同主机仍只靠心跳过期 |
| I2 | API 交给 worker 的租约写的是 API 的进程号 | worker 接手时把 token 换成自己的（`hold_lease(handed_over=...)`），这样 worker 被 kill 后能按 I1 立即续跑；交接时 token 已不在（被别人接手）就报 `LeaseLost` |
| I3 | 偶发卡住的测试（含 `test_stdio_server_via_cli`）的原因 | /diagnosing-bugs 结论见下节：`llm_calls` 追踪每次调用都在执行器线程里新开、关闭一个 SQLite 连接，多线程同时开关同一文件时在 SQLite 3.51.1 内部死锁（WAL 下更容易触发）。修法：每个 `TrackingCallback` 只开一个连接、加锁串行写；`connect` 只在文件还不是 WAL 时切换，切换失败（并发的首次打开）在 busy timeout 内重试 |
| I4 | API worker 的输出放哪 | `<结果库目录>/runs/`（默认即 `data/runs`）：导出文件与 `run_<id>.worker.log`。测试里随临时结果库进临时目录 |
| I5 | worker 在复核运行上会进入 CLI 交互复核并因没有终端而中止 | `rfq run` 加隐藏参数 `--worker`：不交互，红档行留给 API 复核。改 OE 号的 worker 用隐藏命令 `rfq runs answer RUN_ID --decision JSON --lease-token T` |
| I6 | 预计调用数在哪算 | `orchestrator/estimate.py`（`run_calls`、`review_roles`、`real_roles`），CLI 与 API 共用 |
| I7 | schema 版本 | 字段有变化的 `RunRecord`（加 `finished_at`）、`ReviewDecision`（加 `row_id`）升到 `"1.1"`；新模型为 `"1.0"` |

## 偶发卡住的诊断（/diagnosing-bugs，2026-10-08）
- 现象：整套测试偶发卡住，CPU 为 0。本会话复现 3 次，卡在调用 agent CLI 的测试（`test_should_cost`、`test_oe_interchange`），父进程与 MCP 子进程都在等待。
- 反馈回路：在冻结的代码副本上，3 份并行反复跑 `test_oe_interchange.py test_should_cost.py test_mcp_server.py`，`-o faulthandler_timeout=60 -o faulthandler_exit_on_timeout=true`。修复前第一轮 3 份中 1 份卡住。单独循环 MCP stdio 会话 300 次不卡，说明 MCP 本身不是原因。
- 证据：faulthandler 显示多个执行器线程停在 `store/db.py` 的 `connect`（来自 `tracking._write`），主线程在事件循环里等这些回调；另一次卡住的进程用 `sample` 取原生栈：8 个线程在 `sqlite3.connect → unixOpen → findReusableFd` 等互斥锁，1 个线程在关闭连接 `sqlite3WalClose → unixLock` 等互斥锁。
- 结论：每次 LLM 调用新开、关闭一个连接，多线程并发时在 SQLite 内部互相等待。根因在审查修复时查实：SQLite 3.51.0 / 3.51.1 unix VFS 的已知死锁（3.51.2 修复），最终修法是进程内打开 / 关闭连接串行化，见 `phase-8-fixes.md`。修复后同一回路 6 轮 × 3 份全部通过，之后整套测试循环跑也没有再卡住。
- 死锁的回归测试：独立脚本按同样模式（多线程反复开关连接）没能复现死锁，没有能稳定变红的测试接口，所以不加回归测试，由上面的回路验证。
- 第二个问题（同一回路中发现）：修复死锁后整套测试循环 4 次不再卡住，但第 4 次有 1 个测试失败：追踪回调报 “database is locked”，少记了一次调用。原因：新文件第一次切换到 WAL 时要拿排他锁，而且不等 busy timeout，多个线程同时首次打开会失败（脚本测得 16 线程同时首次打开，50 次中 4 次失败）。修法：切换失败时在 busy timeout 内重试。回归测试 `test_many_threads_can_open_a_new_database_at_once`（修复前 3 次中 2 次失败，修复后脚本 200 次 0 失败）。

## 要写回的文档（已完成，2026-10-08）
- RFQ-SPEC.md §13：复核改为在已完成的运行上进行（P2）；interrupt 交出整个队列、`ReviewDecision.row_id`（P1）；运行租约（P4）。§7：`ReviewPrompt` / `ReviewDecision` 字段变化。§15.1：复核接口的 202 语义。
- GLOSSARY.md：运行租约（worker、心跳）。

## 完成记录（2026-10-08）
- fake 模式 `uv run pytest`：459 passed；ruff、`mypy src` 无问题。修复后在代码副本上整套测试连续跑 3 次全部通过，没有再卡住（副本不含 `data/generated`，1 个依赖它的测试跳过）。
- 验收（全部 fake，自动化测试）：工作表列、整行着色、冻结窗格、汇总 sheet（`tests/test_excel.py`）；§15.1 全部接口加 `POST /resume` 的成功与错误码（`tests/test_api.py`）；API 启动真实 worker 子进程后台跑完 200 行，跑到第 3 批后 `kill -9`，`lease_active` 立即变为 false，`POST /resume` 续跑完，导出与一次跑完的 CLI 运行逐字节相同；worker 持租约期间 `rfq run --resume` 以退出码 3 被拒（`test_api_workers_run_in_the_background_and_a_killed_one_resumes`）。
- 手工冒烟：`rfq api serve` + curl 上传 20 行样例（复核模式），worker 收尾到 `awaiting_review`（绿 11 / 黄 5 / 红 4，与 P7 一致），HTTP 拒绝一行后汇总更新，xlsx 可下载，`/docs` 可访问。冒烟中发现 worker 在复核运行上会进入终端交互而中止（I5），已加测试并修复。
- 真实 LLM：P8 没有新增 LLM 调用，未跑。
