# Phase 9 — Streamlit Dashboard

分支：`phase-9-dashboard`（从 main a5e9c15 建）

## 目标与验收（spec §2、§15.2、§17）
六个页面：上传与运行、运行总览、工作表、行详情、复核队列、评估。Dashboard 只通过 HTTP 调 FastAPI（以后换 React），不 import `rfq_agent` 的任何模块。
验收（fake 模式）：每个页面都有测试；有测试证明 dashboard 不 import 后端；启动 `rfq api serve` 与 Dashboard，在页面上走完：上传 20 行样例 → 进度 → 总览 → 工作表 → 行详情 → 复核（含改 OE 号）→ 下载 xlsx。

## 前提
- 后端接口以 P8 已合并的为准（§15.1 + `POST /runs/{id}/resume`），字段用 `/openapi.json` 核对。P9 不改 API；核对后没有发现缺的接口或字段（见 Q14）。
- phase-6.md 末尾的三个待决问题不属于 P9。

## /grilling 决定（2026-10-08）
Q1 由用户确认；其余用户授权按推荐方案定，计划整体交用户审阅。用户审阅后同意全部决定（含 Q1 改为页面测试注入进程内真实 API、Q6 复核默认勾选）和下面的测试接口（2026-10-08）。

### 测试与结构
| # | 问题 | 决定 |
|---|---|---|
| Q1 | 测试方式 | 三层：① 页面测试用 `streamlit.testing.v1.AppTest` 跑真实页面，API 客户端经 `st.session_state["api_client"]` 注入（测试在首次 `run()` 前放入；页面里没有时按 `RFQ_API_URL` 新建）；② `dashboard/api_client.py`（httpx 薄封装）用 `httpx.MockTransport` 测 URL、参数、错误映射；③ 端到端：子进程启动真实 `rfq api serve`（fake、临时端口、临时结果库），真实客户端 + AppTest 走完整流程。页面测试的数据不手写：测试侧（不受“不 import 后端”限制）把 FastAPI `TestClient(create_app(...))` 当作注入的 httpx 客户端（`TestClient` 是 `httpx.Client` 子类），对一个用 fake 跑完的运行发真实请求，schema 一变测试就红；进行中、停止、409、404、连不上等状态用 `MockTransport` 构造。“不 import 后端”两种检查：AST 扫描 `dashboard/**/*.py` 不得出现 `rfq_agent`；子进程在 `sys.path` 去掉 `src`、并让 `import rfq_agent` 失败的情况下导入全部 dashboard 模块 |
| Q2 | 代码结构 | `dashboard/` 是独立包：`app.py`（入口，`st.navigation` 六页 + 侧栏选运行）、`api_client.py`（`ApiClient`、`ApiError(status, detail)`）、`views/`（每页一个模块）、`charts.py`（纯函数：JSON → 图表规格 / DataFrame，可单测）。Dashboard 自己不定义与后端重复的 Pydantic 模型，直接读 JSON 字典（以后换 React 也是按 OpenAPI 读） |
| Q3 | 图表库 | Altair（Streamlit 自带依赖，不加新包）。成本瀑布图由 `charts.py` 算出每段起止（material → conversion → overhead → sga → profit → packaging → FOB 小计 → freight → insurance → duty → mpf → brokerage → inland_freight → landed cost 合计），用 Altair 浮动柱（`y` / `y2`）画；不引入 Plotly |
| Q4 | API 地址与启动 | 环境变量 `RFQ_API_URL`，默认 `http://127.0.0.1:8000`；不读 `config/settings.yaml`（那是后端配置）。启动：`uv run streamlit run dashboard/app.py`，不加 `rfq dashboard` 命令（CLI 属于后端，Dashboard 将被替换）。依赖：`streamlit` 放进新的依赖组 `dashboard`，并设 `[tool.uv] default-groups = ["dev", "dashboard"]`，后端运行时依赖不变 |

### 页面
| # | 问题 | 决定 |
|---|---|---|
| Q5 | 选运行 | 侧栏下拉框列出 `GET /runs`（新的在前，显示 run_id、创建时间、状态），选中的 run_id 存 `st.session_state` 并同步到 URL 查询参数 `?run=`；上传成功后自动选中新运行。除上传页外，未选运行时提示“先选择或上传一个运行” |
| Q6 | 上传参数 | 文件（.xlsx）+ strategy（下拉，默认“服务端默认”即不传）、batch_size（可空）、review（勾选框，**默认勾选**，否则复核队列页无事可做；API 默认仍为 False）、limit（可空）。不开放 `dataset`（服务端路径，不适合给前端）。错误：422 显示服务端 detail（读不了 Excel）；409（真实 provider 而服务端没加 `--allow-real`）显示 detail 原文（含预计调用数），并提示“需要用 `rfq api serve --allow-real` 启动服务端” |
| Q7 | 进度与运行状态 | `st.fragment(run_every=2s)` 轮询 `GET /runs/{id}`，运行不是 `running` 后停止。进度条 =（completed + failed）/ batches_total，另列 pending / running / failed 批数；批全部结束但状态仍为 `running` 显示“收尾中”。状态文案按 §13：`awaiting_review` =“已收尾，工作表可下载，还有 N 个红档行待复核”；`completed` =“已完成”；`running` 且 `lease_active=false` =“后台已停止，可续跑” |
| Q8 | 续跑按钮 | 上传页与总览页在 `running` 且 `lease_active=false` 时显示“续跑”，调 `POST /runs/{id}/resume`；202 后回到轮询；409 显示 detail（被别的进程接手、真实 provider） |
| Q9 | 运行总览 | `RunDetail.summary`：总行数、绿 / 黄 / 红数量、复核数（接受 / 修改 / 拒绝 / 待复核）、四路覆盖率、有行失败的行数、LLM 调用 / token / 成本（为空显示“有未定价模型”）、耗时（注明墙钟，含停机时间）；运行参数（RunSpec）。`summary` 为空时显示进度并提示收尾后才有汇总 |
| Q10 | 工作表 | 服务端分页：`band`、`status` 多选筛选直接传给 API，每页条数 50 / 100 / 500（API 上限 1000），翻页用页码；显示总数。表格用 pandas Styler 按档位整行着色，颜色与 Excel 相同（`C6EFCE` / `FFEB9C` / `FFC7CE`）；列与 §12.3 工作表一致（各站点价格展开为列）。单选一行可跳到行详情。运行已收尾（summary 不为空）时显示“下载工作表 (xlsx)”按钮（`GET worksheet.xlsx`），未收尾不显示 |
| Q11 | 行详情 | 行号输入（或从工作表 / 复核队列带过来）→ `GET rows/{row_id}`。区块：① 结论：参考价区间三列（MSRP、参考零售价、landed cost）、分数与档位、复核状态、需复核原因、行失败；② 替代链：旧 → 新的号码链（当前 OE 高亮），一对多时列出全部候选（含匹配类型、品牌、证据）；③ 各站点价格对比：Altair 柱图，选中、离群、普通三种颜色，MSRP 画一条参考线，下方表格列原价 / 折扣价 / 实际价 / 有货 / 匹配质量；④ 成本瀑布图（`breakdown` 为空时说明原因，如 insufficient_data）；⑤ 置信度信号分解：六个证据信号的条形图（0–1），触发的封顶（`gate_*`）和 LLM 信号单独列出，附 explanation 与策略；⑥ 各阶段 reasoning（四路完整 reasoning 与证据，行失败时显示失败阶段与原因；`RowFailure` 按是否有 `stage` 字段识别）；⑦ 复核历史（`reviews`） |
| Q12 | 复核队列 | 运行未开复核：提示“该运行未开启复核”；状态不是 `awaiting_review`：提示原因。列表取 `GET rows?band=red&status=unreviewed`（全部取完，红档行通常不多；超过 1000 时分页）。选中一行显示要点（需复核原因、参考价、OE 候选）和行详情入口；表单：接受 / 拒绝 / 改价（MSRP、参考零售价至少一个，> 0）/ 改 OE 号（必填）+ 备注。200 → 显示结果、刷新队列；409 → 显示 detail 并刷新队列 |
| Q13 | 改 OE 号 202 之后 | 记下待完成的 `(run_id, row_id)`，`st.fragment(run_every=2s)` 轮询 `GET rows/{row_id}`：`review_status == "modified"` 即完成（显示新分数 / 档位），刷新队列。上限 120 秒：超时显示“仍在后台处理，可稍后刷新”。轮询中若 `GET /runs/{id}` 的 `lease_active=false` 且该行仍未完成，显示“后台已停止：重新提交这一行的同一决定即可完成”（§15.1），并提供“重新提交”按钮 |
| Q14 | 评估 | `GET /runs/{id}/eval`：404 显示“暂无评估”；其他非 2xx 显示错误。不实现 P10 的指标和图表；返回 200 时只原样显示 JSON（格式等 P10 定）|
| Q15 | 接口核对 | 六个页面所需字段都在 P8 接口中：运行列表 / 详情（progress、summary、lease_active）、行分页（band、status、offset、limit）、行详情（ResolvedRow 全部四路、confidence.signals / explanation、reviews、in_review_queue）、复核、xlsx、eval、resume。不改 API。证据信号的权重在服务端配置里，API 没有，页面只展示信号值（不展示权重），不为此加接口 |
| Q16 | 错误与缓存 | `ApiClient` 把非 2xx 统一转成 `ApiError(status, detail)`，连不上时转成 `ApiError(0, "无法连接 API <url>")`，页面用 `st.error` 显示。不使用 `st.cache_data`（复核会改数据，每次请求都取最新）。界面文字中文，代码注释英文 |

## 查证的文档事实（2026-10-08，Streamlit 1.65.0，Python ≥ 3.10；子代理在临时 venv 中实测）
- AppTest：`from_file` / `from_string` / `from_function`，默认超时 3 秒，首次运行冷导入 Altair 会超时，测试用 `run(timeout=30)`。`session_state`、`query_params` 可在首次 `run()` 前设置，`session_state` 可存任意 Python 对象（用于注入客户端）。
- `file_uploader` 支持：`at.file_uploader[0].upload(name, bytes, mime).run()`。
- `download_button` 支持读 label、点击；拿不到文件名和字节，所以 xlsx 字节在 `ApiClient` 测试与端到端测试里检查。`data` 可为无参函数（点击时才取），配 `on_click="ignore"`。
- 多页：`st.navigation` 下先从入口 `run()` 一次，再 `at.switch_page(...)`。
- `st.dataframe` 传 Styler：`at.dataframe[0].value` 拿到底层 DataFrame，样式不可检查（档位着色在 `charts.py` 单测）。
- Altair 图表：`at.get("vega_lite_chart")`，`.proto.spec` 为 JSON 规格。
- `st.fragment(run_every=...)` 在 AppTest 中不会自己按时触发，每次 `at.run()` 执行一次，测试用多次 `run()` 模拟轮询；fragment 内的 session_state 改动会累积，要自己防重复；只重跑 fragment 用 `st.rerun(scope="fragment")`。
- `st.dialog` 在 AppTest 中可见（本计划不用）。
- 依赖：streamlit 自带 altair、pandas、pyarrow；Plotly 不是核心依赖（Q3 因此选 Altair）。Altair 官方画廊有瀑布图示例（累加窗口 + 分层柱）；本计划在 Python 里算好起止再画，便于单测。

## TDD 测试接口（seam，已确认）
1. `dashboard.api_client.ApiClient`：`httpx.MockTransport` 断言每个方法的方法、路径、查询参数 / 表单；错误映射（404、409、422、连不上）。
2. `dashboard.charts`：纯函数——瀑布图各段起止与合计、价格对比的着色分类（selected / outlier / normal）、信号与封顶的拆分、工作表行 → DataFrame 与档位着色。
3. 页面（AppTest，每页一个测试文件）：注入 `TestClient(create_app(...))`（模块级 fixture 用 fake 跑完一个 20 行复核运行）或 `MockTransport` 客户端；断言关键元素与交互。
4. 隔离：AST 扫描 + 去掉后端的子进程导入。
5. 端到端：子进程 `rfq api serve`（fake），真实 `ApiClient` + AppTest 走完 20 行全流程（含改 OE 号与下载 xlsx）。

## 实施顺序
1. 加依赖组、写回文档（下节），单独提交。
2. seam 4 → 1 → 2 → 3（上传 → 总览 → 工作表 → 行详情 → 复核 → 评估）→ 5；脚本替换每处断言匹配一次。
3. 手工验收：启动 `rfq api serve` 与 `streamlit run dashboard/app.py`，在浏览器里走完整流程（fake）。
4. 真实 LLM：P9 不新增 LLM 调用，默认不跑。如需在页面上看一次真实运行，20 行约 85 次调用、约 $0.05（P8 估计），经同意由用户用 `!` 启动 `rfq api serve --allow-real`。

## 要写回的文档（已完成，2026-10-08）
- RFQ-SPEC.md §15.2：页面细则（Q5–Q14 的要点）、启动方式与 `RFQ_API_URL`、不 import 后端的测试；§5：`dashboard/` 目录结构。
- GLOSSARY.md：不新增术语（“后台已停止，可续跑”沿用“续跑”“运行租约”）。

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| I1 | AppTest 怎么跑单个页面 | `AppTest.from_function` 跑一个只调 `dashboard.app.render_view(view)` 的小脚本（页面名经 kwargs 传入），客户端经 `session_state["api_client"]` 注入；整站（导航、侧栏、`?run=`）用 `from_file(app.py)` 测 |
| I2 | 页面之间跳转怎么测 | 六页是 `st.Page` 函数页，`AppTest.switch_page` 只支持文件页，所以“工作表 → 行详情”的跳转（`st.switch_page`）在浏览器验收里检查；测试只检查导航有六页、选中的行号写进 `session_state` |
| I3 | `streamlit run dashboard/app.py` 找不到 `dashboard` 包 | `app.py` 开头把项目根目录加进 `sys.path`（Streamlit 只把脚本所在目录加进去） |
| I4 | 提交类操作的写法（浏览器验收发现） | 上传和复核提交改为按钮的 `on_click` 回调：回调在页面重跑之前执行，侧栏能立即列出新运行，复核表单能重置（回调里给控件 key 赋空值；删除 key 不会清空浏览器里的值）。结果提示存进 `session_state`、直到被下一次结果替换（浏览器在点击后可能多重跑一次，用完即删会丢提示） |
| I5 | 侧栏选中的运行在状态变化后丢失（浏览器验收发现） | 选择框的显示文字随状态变化（“running”→“awaiting_review”）时，浏览器按旧文字回传，选中值变成了显示文字、各页 404。显示文字改为只含 run_id 与创建时间（不含状态），回调只接受列表中的 run_id。AppTest 不模拟前端这一行为，测试固定“显示文字不含状态” |
| I6 | 切换页面时 URL 丢掉 `?run=` | 每次渲染侧栏时把选中的运行写回查询参数 |
| I7 | 表格数字显示 | 工作表 Styler 保留 2 位小数；可能为空的整数列（row_id、价格来源行、分数）用 pandas `Int64`，不会变成浮点。空值在 `st.dataframe` 中显示为 None（Styler 的 `na_rep` 只用于 HTML，未处理） |
| I8 | 上传目录 | `data/uploads/`（P8 的 `storage.upload_dir`）加入 `.gitignore`，P8 漏了 |

## 完成记录（2026-10-08）
- fake 模式 `uv run pytest`：528 passed（P8 合并时 475，新增 53 个 dashboard 测试）；ruff、`mypy src` 无问题；`mypy --ignore-missing-imports dashboard` 无问题（pandas 没有类型存根，dashboard 不在项目的 `mypy src` 范围内）。
- 验收（§17 P9：六个页面可用，只依赖 API）：
  - 每个页面都有测试：`tests/dashboard/test_{upload,overview,worksheet,row_detail,review,evaluation}_page.py`，加整站 `test_app.py`、客户端 `test_api_client.py`、图表与表格 `test_charts.py`。
  - 不 import 后端：`test_isolation.py`（AST 扫描 + 屏蔽 `rfq_agent` 后在子进程导入全部 dashboard 模块；在 dashboard 里放一个 import 后端的文件，两个测试都会变红）。
  - 端到端：`test_end_to_end.py` 启动真实 `rfq api serve` 子进程（fake），页面走完上传 20 行 → 进度 → 总览 → 工作表 → 行详情 → 复核（接受 + 改 OE 号，由 worker 进程完成）→ 评估“暂无评估”→ xlsx（复核状态写在工作表里）。
  - 浏览器手工验收（Claude in Chrome，`rfq api serve` + `streamlit run`，fake）：上传 20 行（复核模式）→ 进度轮询到“已收尾，还有 4 个红档行待复核”（绿 11 / 黄 5 / 红 4，与 P8 一致）→ 总览 → 工作表（档位着色、勾选一行后跳到行详情）→ 行详情（价格对比柱图标出选中 / 离群与 MSRP 线、成本瀑布、信号条形图）→ 复核队列：改 OE 号（202 → 轮询 → “改 OE 号已完成”）、拒绝、接受。下载按钮在页面上可见；按浏览器安全规则没有在浏览器里点击下载，xlsx 内容由端到端测试检查。验收中发现 I4–I7 并修复（先加测试）。
- 真实 LLM：P9 不新增 LLM 调用，未跑。
